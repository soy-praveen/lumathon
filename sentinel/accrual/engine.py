"""Deterministic accrual detection.

Two detections run at close:

- rni_po: goods receipts in the period whose purchase order has no matching AP
  invoice. Fully received POs get a proposed accrual JE (debit expense, credit
  accrued liabilities) that reverses next period. Partial receipts are
  ambiguous, so they become exceptions instead of JEs.
- missing_recurring: active recurring vendors with no AP invoice and no GL
  activity in the period get a proposed accrual at the expected amount.

No LLM calls here. Every proposed JE cites the source rows it was derived from.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db import (
    APInvoice,
    GLEntry,
    GoodsReceipt,
    PurchaseOrder,
    RecurringVendor,
    Vendor,
)
from sentinel.schemas import Evidence, JELine, PolicyRule, ProposedJE

ACCRUED_LIABILITIES_ACCOUNT = "2100"
DEFAULT_EXPENSE_ACCOUNT = "6000"
AMOUNT_TOLERANCE = 0.01  # one cent
AUTO_APPROVE_CONFIDENCE = 0.9
RNI_CONFIDENCE = 0.95
RECURRING_CONFIDENCE = 0.8
RULE_BOOSTED_CONFIDENCE = 0.95


class AccrualResult(BaseModel):
    proposed_jes: list[ProposedJE]
    exceptions: list[dict]


def run_accruals(
    session: Session, period: str, rules: list[PolicyRule] | None = None
) -> AccrualResult:
    """Detect missing accruals for the period and propose evidence-backed JEs."""
    proposed: list[ProposedJE] = []
    exceptions: list[dict] = []
    _detect_rni_pos(session, period, proposed, exceptions)
    _detect_missing_recurring(session, period, rules or [], proposed, exceptions)
    return AccrualResult(proposed_jes=proposed, exceptions=exceptions)


def _next_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def _status_for(confidence: float) -> str:
    return "auto_approved" if confidence >= AUTO_APPROVE_CONFIDENCE else "needs_review"


def _detect_rni_pos(
    session: Session, period: str, proposed: list[ProposedJE], exceptions: list[dict]
) -> None:
    receipts = session.scalars(
        select(GoodsReceipt).where(GoodsReceipt.period == period).order_by(GoodsReceipt.id)
    ).all()
    by_po: dict[int, list[GoodsReceipt]] = defaultdict(list)
    for receipt in receipts:
        by_po[receipt.po_id].append(receipt)

    for po_id in sorted(by_po):
        po = session.get(PurchaseOrder, po_id)
        if po is None:
            continue
        po_receipts = by_po[po_id]
        received = round(sum(r.amount for r in po_receipts), 2)
        if _has_matching_invoice(session, po, received):
            continue

        vendor = session.get(Vendor, po.vendor_id)
        vendor_name = vendor.name if vendor else f"vendor {po.vendor_id}"
        if abs(received - po.amount) <= AMOUNT_TOLERANCE:
            expense_account = DEFAULT_EXPENSE_ACCOUNT
            if vendor and vendor.default_expense_account:
                expense_account = vendor.default_expense_account
            evidence = [
                Evidence(
                    source_table="goods_receipts",
                    row_id=r.id,
                    note=f"goods received {r.receipt_date} for {r.amount:.2f}",
                )
                for r in po_receipts
            ]
            evidence.append(
                Evidence(
                    source_table="purchase_orders",
                    row_id=po.id,
                    note=f"PO {po.po_number} has no matching AP invoice",
                )
            )
            proposed.append(
                ProposedJE(
                    lines=[
                        JELine(account=expense_account, debit=received),
                        JELine(account=ACCRUED_LIABILITIES_ACCOUNT, credit=received),
                    ],
                    evidence=evidence,
                    rule="rni_po",
                    reason=(
                        f"Goods received from {vendor_name} on PO {po.po_number} with no AP "
                        f"invoice by close; accrue {received:.2f} and reverse in "
                        f"{_next_period(period)} when the invoice lands."
                    ),
                    confidence=RNI_CONFIDENCE,
                    status=_status_for(RNI_CONFIDENCE),
                )
            )
        else:
            exceptions.append(
                {
                    "category": "rni_po",
                    "description": (
                        f"Partial receipt on PO {po.po_number} from {vendor_name} "
                        f"(purchase_orders row {po.id}): received {received:.2f} of "
                        f"{po.amount:.2f} with no invoice; accrual amount is ambiguous."
                    ),
                    "source_table": "goods_receipts",
                    "row_id": po_receipts[0].id,
                    "period": period,
                }
            )


def _has_matching_invoice(session: Session, po: PurchaseOrder, received: float) -> bool:
    invoices = session.scalars(
        select(APInvoice).where(APInvoice.vendor_id == po.vendor_id, APInvoice.status != "void")
    ).all()
    return any(
        abs(inv.amount - received) <= AMOUNT_TOLERANCE
        or abs(inv.amount - po.amount) <= AMOUNT_TOLERANCE
        for inv in invoices
    )


def _detect_missing_recurring(
    session: Session,
    period: str,
    rules: list[PolicyRule],
    proposed: list[ProposedJE],
    exceptions: list[dict],
) -> None:
    recurring = session.scalars(
        select(RecurringVendor).where(RecurringVendor.active.is_(True)).order_by(RecurringVendor.id)
    ).all()
    for rv in recurring:
        vendor = session.get(Vendor, rv.vendor_id)
        if _recurring_covered(session, period, rv, vendor):
            continue

        vendor_name = vendor.name if vendor else f"vendor {rv.vendor_id}"
        amount = round(rv.expected_amount, 2)
        evidence = [
            Evidence(
                source_table="recurring_vendors",
                row_id=rv.id,
                note=(
                    f"expected monthly bill of {amount:.2f} "
                    f"around day {rv.expected_day}, not seen in {period}"
                ),
            )
        ]
        if vendor:
            evidence.append(
                Evidence(source_table="vendors", row_id=vendor.id, note=f"vendor {vendor.name}")
            )
        confidence = RECURRING_CONFIDENCE
        if vendor and _rule_matches(rules, period, vendor.name):
            confidence = RULE_BOOSTED_CONFIDENCE
        proposed.append(
            ProposedJE(
                lines=[
                    JELine(account=rv.account_code, debit=amount),
                    JELine(account=ACCRUED_LIABILITIES_ACCOUNT, credit=amount),
                ],
                evidence=evidence,
                rule="missing_recurring",
                reason=(
                    f"No invoice or GL activity for recurring vendor {vendor_name} in "
                    f"{period}; accrue expected {amount:.2f} and reverse in "
                    f"{_next_period(period)}."
                ),
                confidence=confidence,
                status=_status_for(confidence),
            )
        )


def _recurring_covered(
    session: Session, period: str, rv: RecurringVendor, vendor: Vendor | None
) -> bool:
    invoice = session.scalars(
        select(APInvoice).where(
            APInvoice.period == period,
            APInvoice.vendor_id == rv.vendor_id,
            APInvoice.status != "void",
        )
    ).first()
    if invoice is not None:
        return True
    if vendor is None:
        return False
    name = vendor.name.lower()
    entries = session.scalars(
        select(GLEntry).where(GLEntry.period == period, GLEntry.account_code == rv.account_code)
    ).all()
    return any(name in (entry.description or "").lower() for entry in entries)


def _rule_matches(rules: list[PolicyRule], period: str, vendor_name: str) -> bool:
    """A promoted policy rule scoped to accruals that names the vendor raises confidence."""
    name = vendor_name.lower()
    for rule in rules:
        if not rule.active:
            continue
        if rule.expires is not None and rule.expires < period:
            continue
        if rule.scope.lower() not in ("accrual", "missing_recurring"):
            continue
        if name in rule.condition.lower():
            return True
    return False
