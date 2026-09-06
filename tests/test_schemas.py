import pytest
from pydantic import ValidationError

from sentinel.schemas import Evidence, JELine, PolicyRule, ProposedJE

VALID_LINES = [
    JELine(account="6200", debit=1250.75, credit=0.0),
    JELine(account="2100", debit=0.0, credit=1250.75),
]
VALID_EVIDENCE = [Evidence(source_table="bank_lines", row_id=42, note="bank fee descriptor")]


def make_je(**overrides) -> ProposedJE:
    kwargs = {
        "lines": VALID_LINES,
        "evidence": VALID_EVIDENCE,
        "rule": "bank_fee_auto",
        "reason": "Monthly service charge matched to bank descriptor",
        "confidence": 0.97,
        "status": "auto_approved",
    }
    kwargs.update(overrides)
    return ProposedJE(**kwargs)


def test_valid_je_accepted():
    je = make_je()
    assert je.confidence == 0.97
    assert je.evidence[0].source_table == "bank_lines"


def test_empty_evidence_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        make_je(evidence=[])


def test_unbalanced_lines_rejected():
    with pytest.raises(ValidationError, match="balance"):
        make_je(
            lines=[
                JELine(account="6200", debit=100.00),
                JELine(account="2100", credit=99.00),
            ]
        )


def test_empty_lines_rejected():
    with pytest.raises(ValidationError):
        make_je(lines=[])


@pytest.mark.parametrize("confidence", [-0.1, 1.1, 5.0])
def test_bad_confidence_rejected(confidence):
    with pytest.raises(ValidationError):
        make_je(confidence=confidence)


def test_bad_status_rejected():
    with pytest.raises(ValidationError):
        make_je(status="maybe")


def test_policy_rule_defaults():
    rule = PolicyRule(
        scope="vendor:AWS",
        condition="descriptor startswith 'AMZN WEB SERV'",
        action="match_to_vendor_invoice",
        limits={"tolerance_pct": 1.0, "cap_amount": 50000.0},
    )
    assert rule.active is True
    assert rule.expires is None
