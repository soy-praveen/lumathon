"""Fixture-based tests for the bank reconciliation engine."""

import pytest
from sqlalchemy.orm import Session

from sentinel.db import Account, BankLine, DodoPayout, GLEntry, get_engine, init_db
from sentinel.recon import run_recon
from sentinel.schemas import PolicyRule

PERIOD = "2026-01"

ACCOUNTS = [
    ("1000", "Cash - Operating", "asset"),
    ("1090", "Bank Clearing", "asset"),
    ("4000", "Revenue", "revenue"),
    ("4500", "Interest Income", "revenue"),
    ("6100", "Bank Fees", "expense"),
    ("6110", "Payment Processing Fees", "expense"),
    ("6200", "Cloud Hosting", "expense"),
]


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    init_db(engine)
    with Session(engine) as db:
        for code, name, type_ in ACCOUNTS:
            db.add(Account(code=code, name=name, type=type_))
        db.commit()
        yield db


def bank(db, amount, day, descriptor, period=PERIOD):
    line = BankLine(
        period=period, line_date=f"{period}-{day:02d}", descriptor=descriptor, amount=amount
    )
    db.add(line)
    db.commit()
    return line


def gl_cash(db, amount, day, description, period=PERIOD, account="1000"):
    debit, credit = (amount, 0.0) if amount > 0 else (0.0, -amount)
    entry = GLEntry(
        period=period,
        entry_date=f"{period}-{day:02d}",
        account_code=account,
        description=description,
        debit=debit,
        credit=credit,
    )
    db.add(entry)
    db.commit()
    return entry


def je_totals(je):
    return sum(line.debit for line in je.lines), sum(line.credit for line in je.lines)


def test_exact_match(session):
    line = bank(session, -1287.53, 15, "AMZN WEB SERV WA 8XKQ2")
    entry = gl_cash(session, -1287.53, 15, "AMZN WEB SERV WA 8XKQ2 payment")

    result = run_recon(session, PERIOD)

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match["match_type"] == "exact"
    assert match["bank_line_ids"] == [line.id]
    assert match["gl_entry_ids"] == [entry.id]
    assert result.exceptions == []
    assert result.stats["auto_match_rate"] == 1.0
    assert result.stats["matched_count"] == 1


def test_fuzzy_match(session):
    line = bank(session, -450.00, 16, "GUSTO PAYROLL 8891X")
    entry = gl_cash(session, -450.00, 18, "Gusto payroll run")

    result = run_recon(session, PERIOD)

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match["match_type"] == "fuzzy"
    assert match["bank_line_ids"] == [line.id]
    assert match["gl_entry_ids"] == [entry.id]
    assert 0.0 < match["confidence"] < 1.0
    assert result.exceptions == []


def test_one_to_many_match(session):
    line = bank(session, -900.00, 20, "ACH BATCH RELIANT OFFICE")
    parts = [
        gl_cash(session, -300.00, 20, "Reliant Office Supply order 1"),
        gl_cash(session, -250.00, 20, "Reliant Office Supply order 2"),
        gl_cash(session, -350.00, 20, "Reliant Office Supply order 3"),
    ]

    result = run_recon(session, PERIOD)

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match["match_type"] == "one_to_many"
    assert match["bank_line_ids"] == [line.id]
    assert sorted(match["gl_entry_ids"]) == sorted(entry.id for entry in parts)
    assert result.exceptions == []


def test_bank_fee_je(session):
    line = bank(session, -35.00, 28, "MONTHLY SERVICE FEE")

    result = run_recon(session, PERIOD)

    assert len(result.proposed_jes) == 1
    je = result.proposed_jes[0]
    assert je.rule == "bank_fee"
    assert je.status == "auto_approved"
    debits, credits = je_totals(je)
    assert abs(debits - 35.00) < 0.005 and abs(credits - 35.00) < 0.005
    assert je.evidence
    assert je.evidence[0].source_table == "bank_lines"
    assert je.evidence[0].row_id == line.id
    assert result.exceptions == []


def test_interest_je(session):
    line = bank(session, 12.44, 31, "INTEREST PAYMENT")

    result = run_recon(session, PERIOD)

    assert len(result.proposed_jes) == 1
    je = result.proposed_jes[0]
    assert je.rule == "bank_interest"
    assert je.status == "auto_approved"
    debits, credits = je_totals(je)
    assert abs(debits - credits) < 0.005
    assert {ev.source_table for ev in je.evidence} == {"bank_lines"}
    assert je.evidence[0].row_id == line.id
    assert {line.account for line in je.lines} == {"1000", "4500"}


def test_timing_difference_je(session):
    line = bank(session, -2400.00, 30, "CHECK 1050 ACME PROPERTIES")
    other = gl_cash(session, -2400.00, 2, "Check 1050 Acme Properties rent", period="2026-02")

    result = run_recon(session, PERIOD)

    assert len(result.proposed_jes) == 1
    je = result.proposed_jes[0]
    assert je.rule == "timing_difference"
    assert je.status == "needs_review"
    cited = {(ev.source_table, ev.row_id) for ev in je.evidence}
    assert cited == {("bank_lines", line.id), ("gl_entries", other.id)}
    assert result.exceptions == []
    assert result.stats["unmatched_bank"] == 0


def test_dodo_payout_je_and_unmatched_payout_exception(session):
    payout = DodoPayout(
        period=PERIOD,
        payout_date=f"{PERIOD}-22",
        gross_amount=5000.00,
        fee_amount=145.00,
        net_amount=4855.00,
        reference="PO-88231",
    )
    orphan = DodoPayout(
        period=PERIOD,
        payout_date=f"{PERIOD}-25",
        gross_amount=1000.00,
        fee_amount=30.00,
        net_amount=970.00,
        reference="PO-88232",
    )
    session.add_all([payout, orphan])
    session.commit()
    deposit = bank(session, 4855.00, 23, "DODO PAYMENTS PAYOUT 88231")

    result = run_recon(session, PERIOD)

    assert len(result.proposed_jes) == 1
    je = result.proposed_jes[0]
    assert je.rule == "dodo_payout"
    assert je.status == "auto_approved"
    debits, credits = je_totals(je)
    assert abs(debits - 5000.00) < 0.005 and abs(credits - 5000.00) < 0.005
    cited = {(ev.source_table, ev.row_id) for ev in je.evidence}
    assert cited == {("dodo_payouts", payout.id), ("bank_lines", deposit.id)}

    assert len(result.exceptions) == 1
    exc = result.exceptions[0]
    assert exc["category"] == "recon_unmatched"
    assert exc["source_table"] == "dodo_payouts"
    assert exc["row_id"] == orphan.id
    assert exc["period"] == PERIOD
    assert result.stats["unmatched_bank"] == 0


def test_unmatched_bank_and_gl_become_exceptions(session):
    line = bank(session, -777.77, 12, "MYSTERY WIRE OUT")
    entry = gl_cash(session, -555.55, 14, "Unknown disbursement")

    result = run_recon(session, PERIOD)

    assert result.matches == []
    assert result.proposed_jes == []
    by_table = {exc["source_table"]: exc for exc in result.exceptions}
    assert by_table["bank_lines"]["row_id"] == line.id
    assert by_table["gl_entries"]["row_id"] == entry.id
    assert all(exc["category"] == "recon_unmatched" for exc in result.exceptions)
    assert result.stats == {
        "auto_match_rate": 0.0,
        "matched_count": 0,
        "unmatched_bank": 1,
        "unmatched_gl": 1,
    }


def test_policy_rule_turns_unmatched_into_rule_match(session):
    line = bank(session, -1543.21, 8, "AMZN WEB SERV WA 9QQ1")

    baseline = run_recon(session, PERIOD)
    assert baseline.matches == []
    assert baseline.exceptions[0]["source_table"] == "bank_lines"

    rule = PolicyRule(scope="bank", condition="prefix:AMZN WEB SERV", action="book_to:6200")
    result = run_recon(session, PERIOD, rules=[rule])

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match["match_type"] == "rule"
    assert match["bank_line_ids"] == [line.id]
    assert len(result.proposed_jes) == 1
    je = result.proposed_jes[0]
    assert "prefix:AMZN WEB SERV" in je.rule
    assert je.status == "auto_approved"
    assert {jl.account for jl in je.lines} == {"6200", "1000"}
    assert result.exceptions == []


def test_expired_or_capped_rules_do_not_apply(session):
    bank(session, -1543.21, 8, "AMZN WEB SERV WA 9QQ1")
    expired = PolicyRule(
        scope="bank", condition="substring:AMZN WEB SERV", action="book_to:6200",
        expires="2025-12",
    )
    capped = PolicyRule(
        scope="bank", condition="substring:AMZN WEB SERV", action="book_to:6200",
        limits={"max_amount": 500},
    )

    result = run_recon(session, PERIOD, rules=[expired, capped])

    assert result.matches == []
    assert len(result.exceptions) == 1


def test_auto_match_rate_math(session):
    gl_cash(session, -1287.53, 15, "AMZN WEB SERV WA 8XKQ2 payment")
    bank(session, -1287.53, 15, "AMZN WEB SERV WA 8XKQ2")
    bank(session, -35.00, 28, "MONTHLY SERVICE FEE")
    bank(session, -777.77, 12, "MYSTERY WIRE OUT")
    bank(session, 4242.42, 19, "UNKNOWN COUNTERPARTY DEPOSIT")

    result = run_recon(session, PERIOD)

    # 4 bank lines: 1 exact match + 1 fee JE resolved, 2 unmatched
    assert result.stats["matched_count"] == 1
    assert result.stats["unmatched_bank"] == 2
    assert result.stats["unmatched_gl"] == 0
    assert abs(result.stats["auto_match_rate"] - 0.5) < 1e-9


def test_period_validation(session):
    with pytest.raises(ValueError):
        run_recon(session, "January 2026")
