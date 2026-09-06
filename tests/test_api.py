"""API tests: every route against a temp SQLite DB seeded through the ORM.

The distill route is exercised with sentinel.llm.complete_json monkeypatched,
so no test here ever needs a real model.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from sentinel import llm
from sentinel.api import app
from sentinel.db import (
    BankLine,
    ExceptionRecord,
    GLEntry,
    JELineRow,
    PolicyRuleRow,
    ProposedJERow,
    get_engine,
    get_session,
    init_db,
)
from sentinel.schemas import PolicyRule


@pytest.fixture
def db_engine(tmp_path, monkeypatch) -> Engine:
    path = tmp_path / "test.db"
    monkeypatch.setenv("SENTINEL_DB", str(path))
    engine = get_engine(str(path))
    init_db(engine)
    return engine


@pytest.fixture
def client(db_engine) -> TestClient:
    return TestClient(app)


def seed_je(
    session,
    period: str = "2025-08",
    status: str = "auto_approved",
    confidence: float = 0.98,
    evidence: list[dict] | None = None,
) -> ProposedJERow:
    row = ProposedJERow(
        period=period,
        rule="bank_fee",
        reason="Monthly bank service fee",
        confidence=confidence,
        status=status,
        evidence_json=evidence or [{"source_table": "bank_lines", "row_id": 1, "note": None}],
    )
    session.add(row)
    session.flush()
    session.add(JELineRow(je_id=row.id, account_code="6110", debit=45.5, credit=0.0))
    session.add(JELineRow(je_id=row.id, account_code="1000", debit=0.0, credit=45.5))
    return row


def seed_exception(
    session,
    period: str = "2025-08",
    category: str = "recon",
    status: str = "open",
) -> ExceptionRecord:
    record = ExceptionRecord(
        period=period,
        category=category,
        description="Unmatched bank line: CHECKCARD 0812 STAPLES 88.40",
        source_table="bank_lines",
        row_id=1,
        status=status,
    )
    session.add(record)
    session.flush()
    return record


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_close_run_on_empty_db(client):
    response = client.post("/close/run", json={"period": "2025-08", "prior_period": "2025-07"})
    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "2025-08"
    assert body["je_count"] == 0
    assert body["exception_count"] == 0


def test_close_run_rejects_bad_period(client):
    response = client.post("/close/run", json={"period": "August 2025"})
    assert response.status_code == 422


def test_jes_lists_lines_and_evidence(client, db_engine):
    with get_session(db_engine) as session:
        seed_je(session)
        seed_je(session, period="2025-07", status="needs_review", confidence=0.6)
    response = client.get("/jes", params={"period": "2025-08"})
    assert response.status_code == 200
    jes = response.json()
    assert len(jes) == 1
    je = jes[0]
    assert je["status"] == "auto_approved"
    assert je["evidence"] == [{"source_table": "bank_lines", "row_id": 1, "note": None}]
    assert {line["account_code"] for line in je["lines"]} == {"6110", "1000"}
    assert sum(line["debit"] for line in je["lines"]) == pytest.approx(45.5)


def test_jes_filters_by_status(client, db_engine):
    with get_session(db_engine) as session:
        seed_je(session, status="auto_approved")
        seed_je(session, status="needs_review", confidence=0.6)
    jes = client.get("/jes", params={"period": "2025-08", "status": "needs_review"}).json()
    assert len(jes) == 1
    assert jes[0]["status"] == "needs_review"
    assert len(client.get("/jes").json()) == 2


def test_evidence_returns_source_row(client, db_engine):
    with get_session(db_engine) as session:
        session.add(
            BankLine(
                period="2025-08",
                line_date="2025-08-12",
                descriptor="CHECKCARD 0812 STAPLES 88.40",
                amount=-88.4,
            )
        )
    response = client.get("/evidence", params={"source_table": "bank_lines", "row_id": 1})
    assert response.status_code == 200
    row = response.json()
    assert row["descriptor"] == "CHECKCARD 0812 STAPLES 88.40"
    assert row["amount"] == pytest.approx(-88.4)


def test_evidence_unknown_table_is_404(client):
    response = client.get("/evidence", params={"source_table": "audit_log", "row_id": 1})
    assert response.status_code == 404


def test_evidence_missing_row_is_404(client):
    response = client.get("/evidence", params={"source_table": "bank_lines", "row_id": 999})
    assert response.status_code == 404


def test_exceptions_filters(client, db_engine):
    with get_session(db_engine) as session:
        seed_exception(session, period="2025-08", status="open")
        seed_exception(session, period="2025-08", category="anomaly", status="resolved")
        seed_exception(session, period="2025-07")
    assert len(client.get("/exceptions").json()) == 3
    assert len(client.get("/exceptions", params={"period": "2025-08"}).json()) == 2
    only_open = client.get("/exceptions", params={"period": "2025-08", "status": "open"}).json()
    assert len(only_open) == 1
    assert only_open[0]["category"] == "recon"


def test_resolve_requires_reason(client, db_engine):
    with get_session(db_engine) as session:
        record = seed_exception(session)
        exception_id = record.id
    missing = client.post(
        f"/exceptions/{exception_id}/resolve",
        json={"resolution": "approve", "actor": "controller"},
    )
    assert missing.status_code == 422
    blank = client.post(
        f"/exceptions/{exception_id}/resolve",
        json={"resolution": "approve", "reason": "   ", "actor": "controller"},
    )
    assert blank.status_code == 422


def test_resolve_happy_path_then_conflict(client, db_engine):
    with get_session(db_engine) as session:
        record = seed_exception(session)
        exception_id = record.id
    ok = client.post(
        f"/exceptions/{exception_id}/resolve",
        json={"resolution": "approve", "reason": "Verified against the receipt", "actor": "sam"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "resolved"
    assert body["resolution"] == "approve"
    again = client.post(
        f"/exceptions/{exception_id}/resolve",
        json={"resolution": "reject", "reason": "changed my mind", "actor": "sam"},
    )
    assert again.status_code == 409


def test_resolve_unknown_exception_is_404(client):
    response = client.post(
        "/exceptions/999/resolve",
        json={"resolution": "approve", "reason": "whatever", "actor": "sam"},
    )
    assert response.status_code == 404


def test_resolve_invalid_resolution_is_422(client, db_engine):
    with get_session(db_engine) as session:
        record = seed_exception(session)
        exception_id = record.id
    response = client.post(
        f"/exceptions/{exception_id}/resolve",
        json={"resolution": "defer", "reason": "not sure", "actor": "sam"},
    )
    assert response.status_code == 422


def test_distill_promotes_with_fake_llm(client, db_engine, monkeypatch):
    with get_session(db_engine) as session:
        record = seed_exception(session, status="resolved")
        record.resolution = "approve"
        record.resolution_reason = "Staples card purchases are office supplies"
        exception_id = record.id
        # a prior period exists but recon finds nothing in it, so the gate passes
        session.add(
            GLEntry(
                period="2025-07",
                entry_date="2025-07-15",
                account_code="6100",
                description="July rent",
                debit=2500.0,
                credit=0.0,
            )
        )

    fake_rule = PolicyRule(
        scope="descriptor:STAPLES",
        condition="bank descriptor starts with CHECKCARD and contains STAPLES",
        action="book to 6100 office supplies",
        limits={"max_amount": 500.0},
    )
    monkeypatch.setattr(llm, "complete_json", lambda *args, **kwargs: fake_rule)

    response = client.post(f"/exceptions/{exception_id}/distill")
    assert response.status_code == 200
    body = response.json()
    assert body["candidate"]["scope"] == "descriptor:STAPLES"
    assert body["promoted"] is True
    assert body["flips"] == []
    assert body["replayed_periods"] == ["2025-07"]

    rules = client.get("/rules").json()
    assert len(rules) == 1
    assert rules[0]["scope"] == "descriptor:STAPLES"
    assert rules[0]["source_exception_id"] == exception_id
    assert rules[0]["active"] is True


def test_distill_unresolved_exception_is_409(client, db_engine):
    with get_session(db_engine) as session:
        record = seed_exception(session, status="open")
        exception_id = record.id
    response = client.post(f"/exceptions/{exception_id}/distill")
    assert response.status_code == 409


def test_distill_unknown_exception_is_404(client):
    assert client.post("/exceptions/999/distill").status_code == 404


def test_distill_degrades_to_503_on_llm_error(client, db_engine, monkeypatch):
    with get_session(db_engine) as session:
        record = seed_exception(session, status="resolved")
        record.resolution = "approve"
        record.resolution_reason = "fine"
        exception_id = record.id

    def boom(*args, **kwargs):
        raise llm.LLMError("model unavailable")

    monkeypatch.setattr(llm, "complete_json", boom)
    response = client.post(f"/exceptions/{exception_id}/distill")
    assert response.status_code == 503
    assert "model unavailable" in response.json()["detail"]


def test_rules_disable(client, db_engine):
    with get_session(db_engine) as session:
        row = PolicyRuleRow(
            scope="vendor:AWS",
            condition="descriptor starts with AMZN WEB SERV",
            action="match to AWS invoices",
            limits_json={"tolerance_pct": 1.0},
        )
        session.add(row)
        session.flush()
        rule_id = row.id
    response = client.post(f"/rules/{rule_id}/disable")
    assert response.status_code == 200
    assert response.json()["active"] is False
    listed = client.get("/rules").json()
    assert listed[0]["active"] is False
    assert listed[0]["limits"] == {"tolerance_pct": 1.0}


def test_disable_unknown_rule_is_404(client):
    assert client.post("/rules/999/disable").status_code == 404


def test_metrics_math(client, db_engine):
    with get_session(db_engine) as session:
        seed_je(session, status="auto_approved")
        seed_je(session, status="auto_approved")
        seed_je(session, status="needs_review", confidence=0.6)
        seed_exception(session, category="recon", status="open")
        seed_exception(session, category="anomaly", status="open")
        seed_exception(session, category="anomaly", status="resolved")
        seed_je(session, period="2025-07")  # other period, must not count
        session.add(
            PolicyRuleRow(scope="a", condition="b", action="c", active=True)
        )
        session.add(
            PolicyRuleRow(scope="d", condition="e", action="f", active=False)
        )
    body = client.get("/metrics", params={"period": "2025-08"}).json()
    assert body["je_count"] == 3
    assert body["auto_approved_count"] == 2
    assert body["needs_review_count"] == 1
    assert body["exception_count"] == 3
    assert body["open_exception_count"] == 2
    assert body["exceptions_by_category"] == {"recon": 1, "anomaly": 2}
    assert body["exceptions_by_status"] == {"open": 2, "resolved": 1}
    # (1 needs_review + 2 open exceptions) / (3 JEs + 3 exceptions)
    assert body["escalation_rate"] == pytest.approx(0.5)
    assert body["rule_count"] == 2
    assert body["active_rule_count"] == 1


def test_metrics_empty_period_is_all_zero(client):
    body = client.get("/metrics", params={"period": "2025-01"}).json()
    assert body["je_count"] == 0
    assert body["escalation_rate"] == 0.0
