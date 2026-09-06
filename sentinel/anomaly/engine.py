"""Deterministic anomaly checks over AP invoices, vendors, and GL entries.

Four checks, each emitting plain exception dicts pointing at the offending row,
with partner row ids named in the description:

- duplicate_invoice: same vendor, same or near-same amount, close dates,
  different invoice numbers.
- vendor_name_variance: near-duplicate vendor names via difflib.
- round_number_split: two or more invoices from one vendor just under a round
  approval limit, or a single suspiciously round invoice amount.
- out_of_period: gl_entries whose entry_date falls outside their period column.

No LLM calls here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db import APInvoice, GLEntry, Vendor

DUP_DATE_WINDOW_DAYS = 10
DUP_AMOUNT_PCT = 0.01  # amounts within 1 percent (or one cent) count as near-same
NAME_SIMILARITY = 0.85
APPROVAL_LIMITS = (1000.0, 2500.0, 5000.0, 10000.0, 25000.0, 50000.0)
JUST_UNDER_SHARE = 0.9  # an amount in [0.9 * limit, limit) is "just under" the limit
ROUND_AMOUNT_MIN = 5000.0
ROUND_STEP = 1000.0
CENT_TOLERANCE = 0.005


class AnomalyResult(BaseModel):
    exceptions: list[dict]


def run_anomaly(session: Session, period: str) -> AnomalyResult:
    """Run all anomaly checks for the period."""
    exceptions: list[dict] = []
    invoices_by_vendor = _invoices_by_vendor(session, period)
    _check_duplicate_invoices(invoices_by_vendor, period, exceptions)
    _check_vendor_name_variance(session, period, exceptions)
    _check_round_number_split(invoices_by_vendor, period, exceptions)
    _check_out_of_period(session, period, exceptions)
    return AnomalyResult(exceptions=exceptions)


def _invoices_by_vendor(session: Session, period: str) -> dict[int, list[APInvoice]]:
    invoices = session.scalars(
        select(APInvoice)
        .where(APInvoice.period == period, APInvoice.status != "void")
        .order_by(APInvoice.id)
    ).all()
    grouped: dict[int, list[APInvoice]] = defaultdict(list)
    for invoice in invoices:
        grouped[invoice.vendor_id].append(invoice)
    return grouped


def _check_duplicate_invoices(
    invoices_by_vendor: dict[int, list[APInvoice]], period: str, exceptions: list[dict]
) -> None:
    for vendor_id in sorted(invoices_by_vendor):
        for first, second in combinations(invoices_by_vendor[vendor_id], 2):
            if first.invoice_number == second.invoice_number:
                continue
            tolerance = max(0.01, DUP_AMOUNT_PCT * max(abs(first.amount), abs(second.amount)))
            if abs(first.amount - second.amount) > tolerance:
                continue
            days_apart = abs(
                (date.fromisoformat(second.invoice_date) - date.fromisoformat(first.invoice_date))
                .days
            )
            if days_apart > DUP_DATE_WINDOW_DAYS:
                continue
            exceptions.append(
                {
                    "category": "duplicate_invoice",
                    "description": (
                        f"Invoice {second.invoice_number} for {second.amount:.2f} dated "
                        f"{second.invoice_date} looks like a duplicate of "
                        f"{first.invoice_number} for {first.amount:.2f} dated "
                        f"{first.invoice_date} from the same vendor "
                        f"(partner ap_invoices row {first.id})."
                    ),
                    "source_table": "ap_invoices",
                    "row_id": second.id,
                    "period": period,
                }
            )


def _check_vendor_name_variance(session: Session, period: str, exceptions: list[dict]) -> None:
    vendors = session.scalars(select(Vendor).order_by(Vendor.id)).all()
    for first, second in combinations(vendors, 2):
        ratio = SequenceMatcher(None, _normalize_name(first.name), _normalize_name(second.name))
        similarity = ratio.ratio()
        if similarity < NAME_SIMILARITY:
            continue
        exceptions.append(
            {
                "category": "vendor_name_variance",
                "description": (
                    f"Vendor name '{second.name}' is suspiciously close to '{first.name}' "
                    f"(similarity {similarity:.2f}, partner vendors row {first.id}); "
                    "possible duplicate vendor record."
                ),
                "source_table": "vendors",
                "row_id": second.id,
                "period": period,
            }
        )


def _normalize_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in name.lower())
    return " ".join(cleaned.split())


def _check_round_number_split(
    invoices_by_vendor: dict[int, list[APInvoice]], period: str, exceptions: list[dict]
) -> None:
    for vendor_id in sorted(invoices_by_vendor):
        group = invoices_by_vendor[vendor_id]
        for limit in APPROVAL_LIMITS:
            band = [inv for inv in group if JUST_UNDER_SHARE * limit <= inv.amount < limit]
            if len(band) < 2:
                continue
            total = sum(inv.amount for inv in band)
            partners = ", ".join(str(inv.id) for inv in band[1:])
            exceptions.append(
                {
                    "category": "round_number_split",
                    "description": (
                        f"{len(band)} invoices from one vendor each just under the "
                        f"{limit:.0f} approval limit, summing to {total:.2f}; possible "
                        f"split to dodge approval (partner ap_invoices rows {partners})."
                    ),
                    "source_table": "ap_invoices",
                    "row_id": band[0].id,
                    "period": period,
                }
            )
        for invoice in group:
            if invoice.amount < ROUND_AMOUNT_MIN:
                continue
            remainder = invoice.amount % ROUND_STEP
            if min(remainder, ROUND_STEP - remainder) > CENT_TOLERANCE:
                continue
            exceptions.append(
                {
                    "category": "round_number_split",
                    "description": (
                        f"Invoice {invoice.invoice_number} for exactly {invoice.amount:.2f} "
                        "is a suspiciously round amount for a vendor bill."
                    ),
                    "source_table": "ap_invoices",
                    "row_id": invoice.id,
                    "period": period,
                }
            )


def _check_out_of_period(session: Session, period: str, exceptions: list[dict]) -> None:
    entries = session.scalars(
        select(GLEntry).where(GLEntry.period == period).order_by(GLEntry.id)
    ).all()
    for entry in entries:
        if entry.entry_date[:7] == entry.period:
            continue
        exceptions.append(
            {
                "category": "out_of_period",
                "description": (
                    f"GL entry '{entry.description}' dated {entry.entry_date} is posted "
                    f"to period {entry.period}; the entry date falls outside the period."
                ),
                "source_table": "gl_entries",
                "row_id": entry.id,
                "period": period,
            }
        )
