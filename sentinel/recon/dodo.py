"""Dodo Payments payout reconciliation.

Each payout's net amount is matched to a bank deposit within a small date
window. A deposit that the matching passes already tied to a GL cash entry
means the ledger has the payout booked, so the payout is reconciled with
nothing further to propose. A deposit still unmatched yields a JE booking
cash at net, the processing fee to fee expense, and revenue at gross.
Payouts whose split does not tie (gross - fee != net) or that have no
matching deposit anywhere become exceptions.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from sentinel.db import BankLine, DodoPayout
from sentinel.recon.matching import AMOUNT_TOLERANCE, amounts_equal
from sentinel.schemas import Evidence, JELine, ProposedJE

DODO_DATE_WINDOW_DAYS = 5
DODO_CONFIDENCE = 0.9


def reconcile_payouts(
    session: Session,
    period: str,
    bank_lines: list[BankLine],
    accounts: dict[str, str],
    ledgered_deposits: list[BankLine] | None = None,
) -> tuple[list[ProposedJE], list[dict], list[BankLine]]:
    """Match payout net amounts to bank deposits; returns (jes, exceptions, remaining).

    `bank_lines` are the deposits still unmatched after the earlier passes;
    `ledgered_deposits` are bank lines those passes already tied to GL cash
    entries. A payout whose deposit sits in the ledgered list is reconciled
    through the ledger: proposing the payout JE again would double-book it.
    """
    payouts = (
        session.query(DodoPayout)
        .filter(DodoPayout.period == period)
        .order_by(DodoPayout.payout_date, DodoPayout.id)
        .all()
    )
    jes: list[ProposedJE] = []
    exceptions: list[dict] = []
    remaining = list(bank_lines)
    ledgered = list(ledgered_deposits or [])

    for payout in payouts:
        if abs((payout.gross_amount - payout.fee_amount) - payout.net_amount) > AMOUNT_TOLERANCE:
            exceptions.append(
                _exception(
                    payout,
                    period,
                    f"Dodo payout {payout.reference or payout.id} split does not tie: "
                    f"gross {payout.gross_amount:.2f} - fee {payout.fee_amount:.2f} "
                    f"!= net {payout.net_amount:.2f}",
                )
            )
            continue
        deposit = _find_deposit(payout, remaining)
        if deposit is not None:
            remaining.remove(deposit)
            jes.append(_payout_je(payout, deposit, accounts))
            continue
        ledgered_hit = _find_deposit(payout, ledgered)
        if ledgered_hit is not None:
            ledgered.remove(ledgered_hit)
            continue
        exceptions.append(
            _exception(
                payout,
                period,
                f"No bank deposit matches Dodo payout {payout.reference or payout.id} "
                f"net {payout.net_amount:.2f} on {payout.payout_date}",
            )
        )
    return jes, exceptions, remaining


def _find_deposit(payout: DodoPayout, bank_lines: list[BankLine]) -> BankLine | None:
    payout_date = date.fromisoformat(payout.payout_date)
    best: tuple[tuple, BankLine] | None = None
    for line in bank_lines:
        if line.amount <= 0 or not amounts_equal(line.amount, payout.net_amount):
            continue
        days = abs((date.fromisoformat(line.line_date) - payout_date).days)
        if days > DODO_DATE_WINDOW_DAYS:
            continue
        key = (days, line.id)
        if best is None or key < best[0]:
            best = (key, line)
    return best[1] if best else None


def _payout_je(payout: DodoPayout, deposit: BankLine, accounts: dict[str, str]) -> ProposedJE:
    return ProposedJE(
        lines=[
            JELine(account=accounts["cash"], debit=round(payout.net_amount, 2)),
            JELine(account=accounts["processing_fees"], debit=round(payout.fee_amount, 2)),
            JELine(account=accounts["revenue"], credit=round(payout.gross_amount, 2)),
        ],
        evidence=[
            Evidence(
                source_table="dodo_payouts",
                row_id=payout.id,
                note=f"payout {payout.reference or payout.id} on {payout.payout_date}",
            ),
            Evidence(source_table="bank_lines", row_id=deposit.id, note=deposit.descriptor),
        ],
        rule="dodo_payout",
        reason=(
            f"Dodo payout net {payout.net_amount:.2f} matches bank deposit "
            f"'{deposit.descriptor}' on {deposit.line_date}; books gross revenue "
            f"{payout.gross_amount:.2f} and processing fee {payout.fee_amount:.2f}"
        ),
        confidence=DODO_CONFIDENCE,
        status="auto_approved",
    )


def _exception(payout: DodoPayout, period: str, description: str) -> dict:
    return {
        "category": "recon_unmatched",
        "description": description,
        "source_table": "dodo_payouts",
        "row_id": payout.id,
        "period": period,
    }
