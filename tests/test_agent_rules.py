from types import SimpleNamespace

import pytest

from sentinel import llm
from sentinel.agent.rules import distill_rule, promote_rule, replay_rule
from sentinel.db import (
    AuditLog,
    ExceptionRecord,
    PolicyRuleRow,
    get_engine,
    get_session,
    init_db,
)
from sentinel.schemas import Evidence, JELine, PolicyRule, ProposedJE

CANDIDATE = PolicyRule(
    scope="vendor:AWS",
    condition="bank descriptor starts with AMZN WEB SERV",
    action="match to open AWS invoices within 1 percent",
    limits={"amount_cap": 50000.0, "tolerance_pct": 1.0},
)


def make_engine(tmp_path, resolved=True):
    engine = get_engine(str(tmp_path / "rules-test.db"))
    init_db(engine)
    with get_session(engine) as session:
        record = ExceptionRecord(
            period="2026-01",
            category="recon",
            description="unmatched bank line AMZN WEB SERV WA 8XKQ2 - 1287.53",
            source_table="bank_lines",
            row_id=3,
        )
        if resolved:
            record.status = "resolved"
            record.resolution = "approve"
            record.resolution_reason = "AWS descriptor, match to the AWS invoice"
        session.add(record)
        session.flush()
        exception_id = record.id
    return engine, exception_id


def make_je(row_id, status, account="6000", amount=1287.53):
    return ProposedJE(
        lines=[
            JELine(account=account, debit=amount),
            JELine(account="1000", credit=amount),
        ],
        evidence=[Evidence(source_table="bank_lines", row_id=row_id)],
        rule="recon:exact_match",
        reason="matched",
        confidence=0.95,
        status=status,
    )


def has_candidate(rules):
    return any(rule.scope == CANDIDATE.scope for rule in rules or [])


def good_recon(session, period, rules=None):
    """The candidate rule resolves an old exception into a new match."""
    jes = [make_je(1, "auto_approved")]
    exceptions = [
        {
            "category": "recon",
            "description": "unmatched bank line AMZN WEB SERV",
            "source_table": "bank_lines",
            "row_id": 3,
            "period": period,
        }
    ]
    if has_candidate(rules):
        jes.append(make_je(3, "auto_approved"))
        exceptions = []
    return SimpleNamespace(matches=[], proposed_jes=jes, exceptions=exceptions, stats={})


def flipping_recon(session, period, rules=None):
    """The candidate rule flips a previously auto-approved decision."""
    status = "needs_review" if has_candidate(rules) else "auto_approved"
    return SimpleNamespace(
        matches=[], proposed_jes=[make_je(1, status)], exceptions=[], stats={}
    )


def test_distill_rule_prompts_llm_with_resolution(tmp_path, monkeypatch):
    engine, exception_id = make_engine(tmp_path)
    prompts = []

    def fake_complete_json(prompt, schema, system=None, model=None):
        prompts.append(prompt)
        assert schema is PolicyRule
        return CANDIDATE

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    rule = distill_rule(engine, exception_id)
    assert rule == CANDIDATE
    assert "AMZN WEB SERV" in prompts[0]
    assert "AWS descriptor, match to the AWS invoice" in prompts[0]


def test_distill_rule_requires_resolved_exception(tmp_path):
    engine, exception_id = make_engine(tmp_path, resolved=False)
    with pytest.raises(ValueError, match="not resolved"):
        distill_rule(engine, exception_id)


def test_replay_passes_when_no_decision_flips(tmp_path):
    engine, _ = make_engine(tmp_path)
    result = replay_rule(engine, CANDIDATE, ["2025-11", "2025-12"], engines={"recon": good_recon})
    assert result.passed
    assert result.flips == []


def test_replay_fails_when_prior_decision_flips(tmp_path):
    engine, _ = make_engine(tmp_path)
    result = replay_rule(engine, CANDIDATE, ["2025-12"], engines={"recon": flipping_recon})
    assert not result.passed
    assert len(result.flips) == 1
    assert "auto_approved to needs_review" in result.flips[0]


def test_promote_good_rule_persists_row_and_audits(tmp_path):
    engine, exception_id = make_engine(tmp_path)
    result = promote_rule(
        engine, CANDIDATE, exception_id, ["2025-12"], engines={"recon": good_recon}
    )
    assert result.promoted
    with get_session(engine) as session:
        row = session.get(PolicyRuleRow, result.rule_id)
        assert row.scope == "vendor:AWS"
        assert row.source_exception_id == exception_id
        assert row.active is True
        assert abs(row.limits_json["amount_cap"] - 50000.0) < 0.005
        audit = session.query(AuditLog).filter_by(action="rule.promoted").one()
        assert audit.detail["rule_id"] == result.rule_id


def test_promote_flipping_rule_is_rejected(tmp_path):
    engine, exception_id = make_engine(tmp_path)
    result = promote_rule(
        engine, CANDIDATE, exception_id, ["2025-12"], engines={"recon": flipping_recon}
    )
    assert not result.promoted
    assert result.rule_id is None
    assert result.flips
    with get_session(engine) as session:
        assert session.query(PolicyRuleRow).count() == 0
        audit = session.query(AuditLog).filter_by(action="rule.rejected").one()
        assert audit.detail["source_exception_id"] == exception_id
