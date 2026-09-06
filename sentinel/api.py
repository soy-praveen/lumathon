"""HTTP layer for the review UI: thin routes over sentinel.agent and sentinel.db.

The app resolves its database from the SENTINEL_DB env var on every request so
tests and deployments can point it at any SQLite file without reimporting.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from sentinel.agent import close, resolution
from sentinel.agent import rules as agent_rules
from sentinel.db import (
    DEFAULT_DB_PATH,
    Account,
    APInvoice,
    AuditLog,
    BankLine,
    DodoPayout,
    ExceptionRecord,
    GLEntry,
    GoodsReceipt,
    JELineRow,
    PolicyRuleRow,
    ProposedJERow,
    PurchaseOrder,
    RecurringVendor,
    Vendor,
    get_engine,
    get_session,
    init_db,
)
from sentinel.llm import LLMError

app = FastAPI(title="Ledger Sentinel")

HUMAN_ACTOR = "human"

# Only rows from these tables can be served as evidence. Everything the engines
# cite lives here; anything else is a 404, never a raw table read.
EVIDENCE_TABLES = {
    "accounts": Account,
    "gl_entries": GLEntry,
    "bank_lines": BankLine,
    "vendors": Vendor,
    "ap_invoices": APInvoice,
    "purchase_orders": PurchaseOrder,
    "goods_receipts": GoodsReceipt,
    "recurring_vendors": RecurringVendor,
    "dodo_payouts": DodoPayout,
}

_engines: dict[str, Engine] = {}


def get_db() -> Engine:
    """Return a (cached) engine for the current SENTINEL_DB path."""
    path = os.environ.get("SENTINEL_DB", DEFAULT_DB_PATH)
    engine = _engines.get(path)
    if engine is None:
        engine = get_engine(path)
        init_db(engine)
        _engines[path] = engine
    return engine


DB = Annotated[Engine, Depends(get_db)]


class CloseRunRequest(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    prior_period: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class ResolveRequest(BaseModel):
    resolution: str
    reason: str = Field(min_length=1)
    actor: str = Field(min_length=1)


def _row_to_dict(row: object) -> dict:
    data: dict = {}
    for column in sa_inspect(row).mapper.column_attrs:
        value = getattr(row, column.key)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        data[column.key] = value
    return data


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/close/run")
def close_run(body: CloseRunRequest, engine: DB) -> close.CloseResult:
    return close.run_close(engine, body.period, prior_period=body.prior_period)


@app.get("/jes")
def list_jes(engine: DB, period: str | None = None, status: str | None = None) -> list[dict]:
    with get_session(engine) as session:
        query = session.query(ProposedJERow)
        if period:
            query = query.filter(ProposedJERow.period == period)
        if status:
            query = query.filter(ProposedJERow.status == status)
        jes = query.order_by(ProposedJERow.id).all()
        je_ids = [je.id for je in jes]
        lines_by_je: dict[int, list[dict]] = {je_id: [] for je_id in je_ids}
        if je_ids:
            line_rows = (
                session.query(JELineRow)
                .filter(JELineRow.je_id.in_(je_ids))
                .order_by(JELineRow.id)
                .all()
            )
            for line in line_rows:
                lines_by_je[line.je_id].append(
                    {"account_code": line.account_code, "debit": line.debit, "credit": line.credit}
                )
        return [
            {
                "id": je.id,
                "period": je.period,
                "rule": je.rule,
                "reason": je.reason,
                "confidence": je.confidence,
                "status": je.status,
                "created_at": je.created_at.isoformat(),
                "evidence": list(je.evidence_json or []),
                "lines": lines_by_je[je.id],
            }
            for je in jes
        ]


@app.get("/evidence")
def get_evidence(source_table: str, row_id: int, engine: DB) -> dict:
    model = EVIDENCE_TABLES.get(source_table)
    if model is None:
        raise HTTPException(status_code=404, detail=f"unknown evidence table {source_table!r}")
    with get_session(engine) as session:
        row = session.get(model, row_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no row {row_id} in table {source_table!r}"
            )
        return _row_to_dict(row)


@app.get("/exceptions")
def list_exceptions(
    engine: DB, period: str | None = None, status: str | None = None
) -> list[dict]:
    with get_session(engine) as session:
        query = session.query(ExceptionRecord)
        if period:
            query = query.filter(ExceptionRecord.period == period)
        if status:
            query = query.filter(ExceptionRecord.status == status)
        return [_row_to_dict(record) for record in query.order_by(ExceptionRecord.id).all()]


@app.post("/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: int, body: ResolveRequest, engine: DB) -> dict:
    try:
        return resolution.resolve_exception(
            engine, exception_id, body.resolution, body.reason, body.actor
        )
    except ValueError as exc:
        raise HTTPException(status_code=_value_error_status(exc), detail=str(exc)) from exc


@app.post("/exceptions/{exception_id}/distill")
def distill_exception(exception_id: int, engine: DB) -> dict:
    with get_session(engine) as session:
        record = session.get(ExceptionRecord, exception_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"exception {exception_id} not found")
        exception_period = record.period
        periods = session.query(GLEntry.period).distinct().order_by(GLEntry.period).all()
    prior_periods = [p for (p,) in periods if p < exception_period]

    try:
        candidate = agent_rules.distill_rule(engine, exception_id)
    except ValueError as exc:
        raise HTTPException(status_code=_value_error_status(exc), detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"rule distillation failed: {exc}") from exc

    promotion = agent_rules.promote_rule(engine, candidate, exception_id, prior_periods)
    return {
        "candidate": candidate.model_dump(),
        "promoted": promotion.promoted,
        "rule_id": promotion.rule_id,
        "flips": promotion.flips,
        "replayed_periods": prior_periods,
    }


@app.get("/rules")
def list_rules(engine: DB) -> list[dict]:
    with get_session(engine) as session:
        rows = session.query(PolicyRuleRow).order_by(PolicyRuleRow.id).all()
        return [_rule_to_dict(row) for row in rows]


@app.post("/rules/{rule_id}/disable")
def disable_rule(rule_id: int, engine: DB) -> dict:
    with get_session(engine) as session:
        row = session.get(PolicyRuleRow, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
        row.active = False
        session.add(
            AuditLog(actor=HUMAN_ACTOR, action="rule.disabled", detail={"rule_id": rule_id})
        )
        session.flush()
        return _rule_to_dict(row)


@app.get("/metrics")
def metrics(period: str, engine: DB) -> dict:
    with get_session(engine) as session:
        jes = session.query(ProposedJERow).filter(ProposedJERow.period == period).all()
        exceptions = (
            session.query(ExceptionRecord).filter(ExceptionRecord.period == period).all()
        )
        rule_count = session.query(PolicyRuleRow).count()
        active_rule_count = (
            session.query(PolicyRuleRow).filter(PolicyRuleRow.active.is_(True)).count()
        )

        je_count = len(jes)
        auto_approved_count = sum(1 for je in jes if je.status == "auto_approved")
        needs_review_count = sum(1 for je in jes if je.status == "needs_review")

        by_category: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for record in exceptions:
            by_category[record.category] = by_category.get(record.category, 0) + 1
            by_status[record.status] = by_status.get(record.status, 0) + 1

    open_exception_count = by_status.get("open", 0)
    total_decisions = je_count + len(exceptions)
    escalated = needs_review_count + open_exception_count
    escalation_rate = escalated / total_decisions if total_decisions else 0.0

    return {
        "period": period,
        "je_count": je_count,
        "auto_approved_count": auto_approved_count,
        "needs_review_count": needs_review_count,
        "exception_count": len(exceptions),
        "open_exception_count": open_exception_count,
        "exceptions_by_category": by_category,
        "exceptions_by_status": by_status,
        "escalation_rate": escalation_rate,
        "rule_count": rule_count,
        "active_rule_count": active_rule_count,
    }


def _rule_to_dict(row: PolicyRuleRow) -> dict:
    data = _row_to_dict(row)
    data["limits"] = data.pop("limits_json")
    return data


def _value_error_status(exc: ValueError) -> int:
    message = str(exc)
    if "not found" in message:
        return 404
    if "already" in message or "not resolved" in message:
        return 409
    return 422
