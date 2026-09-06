"""Journal entry proposals for bank-only lines.

Heuristics, in order: interest income (deposit whose descriptor carries an
interest token), bank fees (withdrawal whose descriptor carries a fee token),
timing differences (a line whose amount and descriptor match a GL cash entry
posted in a different period). Category strings on the ``rule`` field are the
contract shared with the data generator and evals: ``bank_fee``,
``bank_interest``, ``timing_difference``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from sentinel.db import BankLine, GLEntry
from sentinel.recon.matching import (
    FUZZY_SIMILARITY,
    amounts_equal,
    gl_signed_amount,
    normalize,
    similarity,
)
from sentinel.schemas import Evidence, JELine, ProposedJE

AUTO_APPROVE_THRESHOLD = 0.85
FEE_CONFIDENCE = 0.9
INTEREST_CONFIDENCE = 0.9
TIMING_CONFIDENCE = 0.7

FEE_TOKENS = {"FEE", "FEES", "CHARGE", "CHRG", "SVC"}
INTEREST_TOKENS = {"INTEREST"}


def _status(confidence: float) -> str:
    return "auto_approved" if confidence >= AUTO_APPROVE_THRESHOLD else "needs_review"


def propose_bank_only_jes(
    session: Session,
    period: str,
    bank_lines: list[BankLine],
    accounts: dict[str, str],
    cash_codes: list[str],
) -> tuple[list[ProposedJE], list[BankLine]]:
    """Propose JEs for unmatched bank lines; returns (jes, still_unmatched)."""
    other_period_gl = (
        session.query(GLEntry)
        .filter(GLEntry.period != period, GLEntry.account_code.in_(cash_codes))
        .order_by(GLEntry.period, GLEntry.id)
        .all()
    )
    used_gl: set[int] = set()

    jes: list[ProposedJE] = []
    remaining: list[BankLine] = []
    for line in bank_lines:
        je = (
            _interest_je(line, accounts)
            or _fee_je(line, accounts)
            or _timing_je(line, other_period_gl, used_gl, accounts)
        )
        if je is None:
            remaining.append(line)
        else:
            jes.append(je)
    return jes, remaining


def _interest_je(line: BankLine, accounts: dict[str, str]) -> ProposedJE | None:
    tokens = set(normalize(line.descriptor).split())
    if line.amount <= 0 or not (tokens & INTEREST_TOKENS):
        return None
    amount = round(line.amount, 2)
    return ProposedJE(
        lines=[
            JELine(account=accounts["cash"], debit=amount),
            JELine(account=accounts["interest_income"], credit=amount),
        ],
        evidence=[Evidence(source_table="bank_lines", row_id=line.id, note=line.descriptor)],
        rule="bank_interest",
        reason=f"Bank deposit '{line.descriptor}' on {line.line_date} is interest income",
        confidence=INTEREST_CONFIDENCE,
        status=_status(INTEREST_CONFIDENCE),
    )


def _fee_je(line: BankLine, accounts: dict[str, str]) -> ProposedJE | None:
    tokens = set(normalize(line.descriptor).split())
    if line.amount >= 0 or not (tokens & FEE_TOKENS):
        return None
    amount = round(-line.amount, 2)
    return ProposedJE(
        lines=[
            JELine(account=accounts["bank_fees"], debit=amount),
            JELine(account=accounts["cash"], credit=amount),
        ],
        evidence=[Evidence(source_table="bank_lines", row_id=line.id, note=line.descriptor)],
        rule="bank_fee",
        reason=f"Bank withdrawal '{line.descriptor}' on {line.line_date} is a bank fee",
        confidence=FEE_CONFIDENCE,
        status=_status(FEE_CONFIDENCE),
    )


def _timing_je(
    line: BankLine,
    other_period_gl: list[GLEntry],
    used_gl: set[int],
    accounts: dict[str, str],
) -> ProposedJE | None:
    counterpart = None
    for entry in other_period_gl:
        if entry.id in used_gl:
            continue
        if not amounts_equal(line.amount, gl_signed_amount(entry)):
            continue
        if similarity(line.descriptor, entry.description) < FUZZY_SIMILARITY:
            continue
        counterpart = entry
        break
    if counterpart is None:
        return None
    used_gl.add(counterpart.id)
    amount = round(abs(line.amount), 2)
    if line.amount > 0:
        lines = [
            JELine(account=accounts["cash"], debit=amount),
            JELine(account=accounts["clearing"], credit=amount),
        ]
    else:
        lines = [
            JELine(account=accounts["clearing"], debit=amount),
            JELine(account=accounts["cash"], credit=amount),
        ]
    return ProposedJE(
        lines=lines,
        evidence=[
            Evidence(source_table="bank_lines", row_id=line.id, note=line.descriptor),
            Evidence(
                source_table="gl_entries",
                row_id=counterpart.id,
                note=f"GL posted in {counterpart.period}",
            ),
        ],
        rule="timing_difference",
        reason=(
            f"Bank line '{line.descriptor}' on {line.line_date} cleared this period but "
            f"GL recorded it in {counterpart.period}"
        ),
        confidence=TIMING_CONFIDENCE,
        status=_status(TIMING_CONFIDENCE),
    )
