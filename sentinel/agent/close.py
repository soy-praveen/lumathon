"""Close orchestration: run the deterministic engines for a period, triage the
proposed JEs, persist everything with evidence, and audit each action.

Engines are imported lazily so this package stays importable while the engine
packages are built in parallel; tests inject fakes through the `engines` dict.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from sentinel import tracing
from sentinel.agent import ambiguity
from sentinel.db import (
    AuditLog,
    ExceptionRecord,
    JELineRow,
    PolicyRuleRow,
    ProposedJERow,
    get_session,
)
from sentinel.schemas import PolicyRule, ProposedJE

AGENT_ACTOR = "agent"

_ENGINE_SPECS = {
    "recon": ("sentinel.recon", "run_recon"),
    "accrual": ("sentinel.accrual", "run_accruals"),
    "flux": ("sentinel.flux", "run_flux"),
    "anomaly": ("sentinel.anomaly", "run_anomaly"),
}


class CloseResult(BaseModel):
    period: str
    je_count: int
    auto_approved_count: int
    needs_review_count: int
    exception_count: int
    stats: dict = Field(default_factory=dict)


def resolve_engine(name: str, engines: dict | None) -> Any:
    """Return the engine callable from the injected dict, else a lazy import."""
    if engines and name in engines:
        return engines[name]
    module_name, attr = _ENGINE_SPECS[name]
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"engine '{name}' is unavailable: could not import {module_name} ({exc}); "
            f"pass engines={{'{name}': fn}} to inject an implementation"
        ) from exc
    return getattr(module, attr)


def load_active_rules(session: Session, period: str) -> list[PolicyRule]:
    """Return active policy rules whose expiry, if any, covers `period`."""
    rows = session.query(PolicyRuleRow).filter(PolicyRuleRow.active.is_(True)).all()
    rules: list[PolicyRule] = []
    for row in rows:
        if row.expires is not None and row.expires < period:
            continue
        rules.append(
            PolicyRule(
                scope=row.scope,
                condition=row.condition,
                action=row.action,
                limits=row.limits_json,
                active=row.active,
                expires=row.expires,
            )
        )
    return rules


def run_close(
    engine: Engine,
    period: str,
    prior_period: str | None = None,
    engines: dict | None = None,
) -> CloseResult:
    """Run the month-end close for `period` and persist the results.

    Calls recon, accrual, flux, and anomaly (flux only when `prior_period` is
    given, since it compares against it), triages proposed JEs through the
    documented confidence bands, and writes proposed_jes, je_lines,
    exceptions, and audit_log rows.
    """
    with tracing.traced_run(f"close:{period}"):
        with get_session(engine) as session:
            rules = load_active_rules(session, period)

            recon_res = resolve_engine("recon", engines)(session, period, rules=rules)
            accrual_res = resolve_engine("accrual", engines)(session, period, rules=rules)
            flux_res = None
            if prior_period is not None:
                flux_res = resolve_engine("flux", engines)(session, period, prior_period)
            anomaly_res = resolve_engine("anomaly", engines)(session, period)

            proposed = list(recon_res.proposed_jes) + list(accrual_res.proposed_jes)
            triaged = ambiguity.triage_jes(proposed)

            auto_approved = 0
            needs_review = 0
            for je in triaged:
                je_id = _persist_je(session, period, je)
                session.add(
                    AuditLog(
                        actor=AGENT_ACTOR,
                        action="je.proposed",
                        detail={
                            "je_id": je_id,
                            "period": period,
                            "rule": je.rule,
                            "status": je.status,
                            "confidence": je.confidence,
                        },
                    )
                )
                if je.status == "auto_approved":
                    auto_approved += 1
                elif je.status == "needs_review":
                    needs_review += 1

            exception_dicts = list(recon_res.exceptions) + list(accrual_res.exceptions)
            if flux_res is not None:
                exception_dicts += list(flux_res.exceptions)
            exception_dicts += list(anomaly_res.exceptions)
            for item in exception_dicts:
                record = ExceptionRecord(
                    period=item.get("period", period),
                    category=item["category"],
                    description=str(item["description"])[:500],
                    source_table=item.get("source_table"),
                    row_id=item.get("row_id"),
                )
                session.add(record)
                session.flush()
                session.add(
                    AuditLog(
                        actor=AGENT_ACTOR,
                        action="exception.opened",
                        detail={
                            "exception_id": record.id,
                            "period": record.period,
                            "category": record.category,
                        },
                    )
                )

            stats: dict = {
                "rules_applied": len(rules),
                "recon": dict(getattr(recon_res, "stats", None) or {}),
            }
            if flux_res is not None:
                stats["flux_movements"] = len(flux_res.movements)

            result = CloseResult(
                period=period,
                je_count=len(triaged),
                auto_approved_count=auto_approved,
                needs_review_count=needs_review,
                exception_count=len(exception_dicts),
                stats=stats,
            )
            session.add(
                AuditLog(actor=AGENT_ACTOR, action="close.completed", detail=result.model_dump())
            )
            return result


def _persist_je(session: Session, period: str, je: ProposedJE) -> int:
    row = ProposedJERow(
        period=period,
        rule=je.rule[:120],
        reason=je.reason[:500],
        confidence=je.confidence,
        status=je.status,
        evidence_json=[item.model_dump() for item in je.evidence],
    )
    session.add(row)
    session.flush()
    for line in je.lines:
        session.add(
            JELineRow(
                je_id=row.id,
                account_code=line.account,
                debit=line.debit,
                credit=line.credit,
            )
        )
    return row.id
