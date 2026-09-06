import pytest

from sentinel import llm
from sentinel.agent import thresholds
from sentinel.agent.ambiguity import AmbiguityVerdict, triage_je
from sentinel.schemas import Evidence, JELine, ProposedJE


def make_je(confidence, rule="recon:fuzzy_match", status="needs_review"):
    return ProposedJE(
        lines=[
            JELine(account="6000", debit=412.88),
            JELine(account="1000", credit=412.88),
        ],
        evidence=[Evidence(source_table="bank_lines", row_id=3)],
        rule=rule,
        reason="fuzzy amount match within tolerance",
        confidence=confidence,
        status=status,
    )


def test_accept_verdict_auto_approves_and_records_rationale(monkeypatch):
    def fake_complete_json(prompt, schema, system=None, model=None):
        assert schema is AmbiguityVerdict
        return AmbiguityVerdict(
            verdict="accept", adjusted_confidence=0.92, rationale="descriptor matches vendor"
        )

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    result = triage_je(make_je(0.70))
    assert result.status == "auto_approved"
    assert result.confidence == pytest.approx(0.92)
    assert "llm accept: descriptor matches vendor" in result.reason


def test_reject_verdict_rejects(monkeypatch):
    monkeypatch.setattr(
        llm,
        "complete_json",
        lambda *a, **k: AmbiguityVerdict(verdict="reject", rationale="amount mismatch"),
    )
    result = triage_je(make_je(0.60))
    assert result.status == "rejected"
    assert "llm reject" in result.reason


def test_escalate_verdict_needs_review(monkeypatch):
    monkeypatch.setattr(
        llm,
        "complete_json",
        lambda *a, **k: AmbiguityVerdict(verdict="escalate", rationale="two candidates"),
    )
    result = triage_je(make_je(0.60))
    assert result.status == "needs_review"


def test_llm_error_degrades_to_escalation(monkeypatch):
    def fake_complete_json(*args, **kwargs):
        raise llm.LLMError("model unavailable")

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    result = triage_je(make_je(0.70))
    assert result.status == "needs_review"
    assert "llm error, escalated" in result.reason


def test_verdict_cannot_change_lines_or_evidence(monkeypatch):
    monkeypatch.setattr(
        llm,
        "complete_json",
        lambda *a, **k: AmbiguityVerdict(verdict="accept", adjusted_confidence=0.9),
    )
    je = make_je(0.70)
    result = triage_je(je)
    assert result.lines == je.lines
    assert result.evidence == je.evidence


def test_high_confidence_skips_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("LLM must not be called above the auto-approve threshold")

    monkeypatch.setattr(llm, "complete_json", boom)
    je = make_je(0.95, status="auto_approved")
    result = triage_je(je)
    assert result.status == "auto_approved"
    assert result.reason == je.reason


def test_below_floor_escalates_without_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("LLM must not be called below the escalation floor")

    monkeypatch.setattr(llm, "complete_json", boom)
    result = triage_je(make_je(0.40, status="auto_approved"))
    assert result.status == "needs_review"
    assert "below escalation floor" in result.reason


def test_rule_backed_je_uses_lower_threshold(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("rule-backed JE above 0.70 must not go to the LLM")

    monkeypatch.setattr(llm, "complete_json", boom)
    je = make_je(0.75, rule="policy:aws-descriptor", status="auto_approved")
    assert thresholds.is_rule_backed(je)
    result = triage_je(je)
    assert result.status == "auto_approved"


def test_same_confidence_without_rule_backing_goes_to_llm(monkeypatch):
    calls = []

    def fake_complete_json(*args, **kwargs):
        calls.append(1)
        return AmbiguityVerdict(verdict="escalate", rationale="not rule backed")

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    result = triage_je(make_je(0.75, status="auto_approved"))
    assert calls
    assert result.status == "needs_review"
