from types import SimpleNamespace

import pytest

from sentinel import llm
from sentinel.agent.close import CloseResult, load_active_rules, run_close
from sentinel.db import (
    AuditLog,
    ExceptionRecord,
    JELineRow,
    PolicyRuleRow,
    ProposedJERow,
    get_engine,
    get_session,
    init_db,
)
from sentinel.schemas import Evidence, JELine, ProposedJE

CENT = 0.005


def make_engine(tmp_path):
    engine = get_engine(str(tmp_path / "close-test.db"))
    init_db(engine)
    return engine


def make_je(confidence, status, row_id=1, amount=125.37):
    return ProposedJE(
        lines=[
            JELine(account="6000", debit=amount),
            JELine(account="1000", credit=amount),
        ],
        evidence=[Evidence(source_table="bank_lines", row_id=row_id, note="matched descriptor")],
        rule="recon:exact_match",
        reason="bank line matched GL cash entry",
        confidence=confidence,
        status=status,
    )


def make_fake_engines(recon_jes=(), recon_exceptions=(), accrual_jes=(), flux_exceptions=(),
                      anomaly_exceptions=(), seen_rules=None):
    def recon(session, period, rules=None):
        if seen_rules is not None:
            seen_rules.extend(rules or [])
        return SimpleNamespace(
            matches=[],
            proposed_jes=list(recon_jes),
            exceptions=list(recon_exceptions),
            stats={"auto_match_rate": 0.9},
        )

    def accrual(session, period, rules=None):
        return SimpleNamespace(proposed_jes=list(accrual_jes), exceptions=[])

    def flux(session, period, prior_period, threshold_pct=10.0, threshold_abs=5000.0):
        return SimpleNamespace(movements=[{"account": "6000"}], exceptions=list(flux_exceptions))

    def anomaly(session, period):
        return SimpleNamespace(exceptions=list(anomaly_exceptions))

    return {"recon": recon, "accrual": accrual, "flux": flux, "anomaly": anomaly}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("run_close must not call the LLM outside the ambiguity band")

    monkeypatch.setattr(llm, "complete_json", boom)
    monkeypatch.setattr(llm, "complete", boom)


def test_run_close_persists_jes_exceptions_and_audit(tmp_path):
    engine = make_engine(tmp_path)
    engines = make_fake_engines(
        recon_jes=[make_je(0.95, "auto_approved", row_id=1)],
        recon_exceptions=[
            {
                "category": "recon",
                "description": "unmatched bank line WIRE OUT 2214",
                "source_table": "bank_lines",
                "row_id": 7,
                "period": "2026-02",
            }
        ],
        accrual_jes=[make_je(0.30, "needs_review", row_id=2, amount=980.00)],
        flux_exceptions=[
            {
                "category": "flux",
                "description": "unexplained movement in 6100",
                "source_table": "gl_entries",
                "row_id": 41,
                "period": "2026-02",
            }
        ],
        anomaly_exceptions=[
            {
                "category": "anomaly",
                "description": "duplicate invoice INV-4482",
                "source_table": "ap_invoices",
                "row_id": 12,
                "period": "2026-02",
            }
        ],
    )

    result = run_close(engine, "2026-02", prior_period="2026-01", engines=engines)

    assert isinstance(result, CloseResult)
    assert result.period == "2026-02"
    assert result.je_count == 2
    assert result.auto_approved_count == 1
    assert result.needs_review_count == 1
    assert result.exception_count == 3
    assert result.stats["recon"] == {"auto_match_rate": 0.9}
    assert result.stats["flux_movements"] == 1

    with get_session(engine) as session:
        je_rows = session.query(ProposedJERow).order_by(ProposedJERow.id).all()
        assert len(je_rows) == 2
        assert je_rows[0].status == "auto_approved"
        assert je_rows[0].evidence_json == [
            {"source_table": "bank_lines", "row_id": 1, "note": "matched descriptor"}
        ]
        assert je_rows[1].status == "needs_review"

        lines = session.query(JELineRow).filter_by(je_id=je_rows[0].id).all()
        assert len(lines) == 2
        assert abs(sum(line.debit for line in lines) - 125.37) < CENT
        assert abs(sum(line.credit for line in lines) - 125.37) < CENT

        exceptions = session.query(ExceptionRecord).all()
        assert len(exceptions) == 3
        assert all(record.status == "open" for record in exceptions)
        assert {record.category for record in exceptions} == {"recon", "flux", "anomaly"}
        anomaly_row = next(r for r in exceptions if r.category == "anomaly")
        assert anomaly_row.source_table == "ap_invoices"
        assert anomaly_row.row_id == 12

        actions = [entry.action for entry in session.query(AuditLog).all()]
        assert actions.count("je.proposed") == 2
        assert actions.count("exception.opened") == 3
        assert actions.count("close.completed") == 1


def test_run_close_skips_flux_without_prior_period(tmp_path):
    engine = make_engine(tmp_path)

    def flux(session, period, prior_period, **kwargs):
        raise AssertionError("flux must not run without a prior period")

    engines = make_fake_engines()
    engines["flux"] = flux
    result = run_close(engine, "2026-01", engines=engines)
    assert result.exception_count == 0
    assert "flux_movements" not in result.stats


def test_run_close_passes_active_unexpired_rules(tmp_path):
    engine = make_engine(tmp_path)
    with get_session(engine) as session:
        session.add(PolicyRuleRow(scope="vendor:AWS", condition="c1", action="a1"))
        session.add(
            PolicyRuleRow(scope="vendor:Old", condition="c2", action="a2", expires="2025-12")
        )
        session.add(
            PolicyRuleRow(scope="vendor:Off", condition="c3", action="a3", active=False)
        )

    seen_rules = []
    engines = make_fake_engines(seen_rules=seen_rules)
    run_close(engine, "2026-02", engines=engines)
    assert [rule.scope for rule in seen_rules] == ["vendor:AWS"]


def test_load_active_rules_keeps_rule_expiring_in_period(tmp_path):
    engine = make_engine(tmp_path)
    with get_session(engine) as session:
        session.add(
            PolicyRuleRow(scope="vendor:Edge", condition="c", action="a", expires="2026-02")
        )
        rules = load_active_rules(session, "2026-02")
        assert [rule.scope for rule in rules] == ["vendor:Edge"]


def test_run_close_missing_engine_raises_clear_error(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)

    def missing(name):
        raise ImportError(f"No module named '{name}'")

    monkeypatch.setattr("sentinel.agent.close.import_module", missing)
    with pytest.raises(RuntimeError, match="sentinel.recon"):
        run_close(engine, "2026-02", engines={})
