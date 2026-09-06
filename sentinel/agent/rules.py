"""The learning loop: distill a policy rule from a resolved exception, replay
it against prior periods as a deterministic regression gate, and promote it
only when no previously correct decision flips.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from sentinel import llm
from sentinel.agent.close import AGENT_ACTOR, load_active_rules, resolve_engine
from sentinel.db import AuditLog, ExceptionRecord, PolicyRuleRow, get_session
from sentinel.schemas import PolicyRule, ProposedJE

_DISTILL_SYSTEM = (
    "You turn one resolved month-end close exception into one narrow, "
    "guardrailed policy rule. Scope it as tightly as possible and set limits "
    "so the rule cannot overreach. Never draft a rule broader than the "
    "decision the human actually made."
)


class ReplayResult(BaseModel):
    passed: bool
    flips: list[str] = Field(default_factory=list)


class PromotionResult(BaseModel):
    promoted: bool
    rule_id: int | None = None
    flips: list[str] = Field(default_factory=list)


def distill_rule(engine: Engine, exception_id: int) -> PolicyRule:
    """Draft a candidate policy rule from a resolved exception via the LLM."""
    with get_session(engine) as session:
        record = session.get(ExceptionRecord, exception_id)
        if record is None:
            raise ValueError(f"exception {exception_id} not found")
        if record.status != "resolved":
            raise ValueError(f"exception {exception_id} is not resolved; resolve it first")
        context = {
            "period": record.period,
            "category": record.category,
            "description": record.description,
            "source_table": record.source_table,
            "row_id": record.row_id,
            "resolution": record.resolution,
            "resolution_reason": record.resolution_reason,
        }
    prompt = (
        "A human resolved this month-end close exception:\n"
        f"{json.dumps(context, indent=2)}\n\n"
        "Draft one narrow policy rule that would let the agent handle the same "
        "situation automatically next month. State scope (for example a vendor "
        "or descriptor), the condition, and the action precisely, and set "
        "limits such as amount caps or tolerances."
    )
    rule = llm.complete_json(prompt, PolicyRule, system=_DISTILL_SYSTEM)
    return PolicyRule.model_validate(rule)


def replay_rule(
    engine: Engine,
    rule: PolicyRule,
    prior_periods: list[str],
    engines: dict | None = None,
) -> ReplayResult:
    """Deterministic regression gate: rerun recon on prior periods with and
    without the candidate rule and report every decision that flips.

    A flip is a baseline JE that disappears or changes status under the
    candidate, or an exception the candidate newly introduces. New matches the
    candidate adds on top of the baseline are allowed; that is the point.
    """
    recon = resolve_engine("recon", engines)
    flips: list[str] = []
    for period in prior_periods:
        with get_session(engine) as session:
            baseline_rules = load_active_rules(session, period)
            base = recon(session, period, rules=baseline_rules)
            cand = recon(session, period, rules=[*baseline_rules, rule])
        base_jes = {_je_key(je): je.status for je in base.proposed_jes}
        cand_jes = {_je_key(je): je.status for je in cand.proposed_jes}
        for key, status in base_jes.items():
            cand_status = cand_jes.get(key)
            if cand_status is None:
                flips.append(f"{period}: baseline JE {key} disappeared under the candidate")
            elif cand_status != status:
                flips.append(
                    f"{period}: JE {key} status changed from {status} to {cand_status}"
                )
        base_exceptions = {_exception_key(item) for item in base.exceptions}
        for item in cand.exceptions:
            key = _exception_key(item)
            if key not in base_exceptions:
                flips.append(f"{period}: candidate introduced a new exception {key}")
    return ReplayResult(passed=not flips, flips=flips)


def promote_rule(
    engine: Engine,
    rule: PolicyRule,
    source_exception_id: int,
    prior_periods: list[str],
    engines: dict | None = None,
) -> PromotionResult:
    """Persist the candidate as a PolicyRuleRow only when the gate passes."""
    replay = replay_rule(engine, rule, prior_periods, engines=engines)
    with get_session(engine) as session:
        if not replay.passed:
            session.add(
                AuditLog(
                    actor=AGENT_ACTOR,
                    action="rule.rejected",
                    detail={
                        "source_exception_id": source_exception_id,
                        "scope": rule.scope,
                        "flips": replay.flips,
                    },
                )
            )
            return PromotionResult(promoted=False, flips=replay.flips)
        row = PolicyRuleRow(
            scope=rule.scope,
            condition=rule.condition,
            action=rule.action,
            limits_json=rule.limits,
            active=rule.active,
            expires=rule.expires,
            source_exception_id=source_exception_id,
        )
        session.add(row)
        session.flush()
        session.add(
            AuditLog(
                actor=AGENT_ACTOR,
                action="rule.promoted",
                detail={
                    "rule_id": row.id,
                    "source_exception_id": source_exception_id,
                    "scope": rule.scope,
                },
            )
        )
        return PromotionResult(promoted=True, rule_id=row.id)


def _je_key(je: ProposedJE) -> tuple:
    """Identity of a decision: cited evidence rows plus touched accounts.

    Amounts stay out of the key so float noise cannot fake a flip; a real
    change in what an entry books shows up through evidence or accounts.
    """
    evidence = tuple(sorted((item.source_table, item.row_id) for item in je.evidence))
    accounts = tuple(sorted(line.account for line in je.lines))
    return (evidence, accounts)


def _exception_key(item: dict) -> tuple:
    return (item.get("category"), item.get("source_table"), item.get("row_id"))
