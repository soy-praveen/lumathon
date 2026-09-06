from sqlalchemy.orm import Session

from sentinel.accrual import run_accruals
from sentinel.accrual.engine import ACCRUED_LIABILITIES_ACCOUNT
from sentinel.db import (
    APInvoice,
    GLEntry,
    GoodsReceipt,
    PurchaseOrder,
    RecurringVendor,
    Vendor,
    get_engine,
    init_db,
)
from sentinel.schemas import PolicyRule

PERIOD = "2026-01"
EXCEPTION_KEYS = {"category", "description", "source_table", "row_id", "period"}


def make_session() -> Session:
    engine = get_engine(":memory:")
    init_db(engine)
    return Session(engine)


def seed_rni_po(session, *, invoiced=False, receipt_amount=None):
    vendor = Vendor(
        name="Globex Manufacturing", payment_terms="net30", default_expense_account="6100"
    )
    session.add(vendor)
    session.flush()
    po = PurchaseOrder(
        period=PERIOD,
        vendor_id=vendor.id,
        po_number="PO-2026-0114",
        order_date="2026-01-06",
        amount=8420.75,
        status="open",
    )
    session.add(po)
    session.flush()
    receipt = GoodsReceipt(
        period=PERIOD,
        po_id=po.id,
        receipt_date="2026-01-19",
        amount=receipt_amount if receipt_amount is not None else 8420.75,
    )
    session.add(receipt)
    if invoiced:
        session.add(
            APInvoice(
                period=PERIOD,
                vendor_id=vendor.id,
                invoice_number="GLX-88412",
                invoice_date="2026-01-21",
                amount=8420.75,
            )
        )
    session.commit()
    return po, receipt


def seed_recurring(session, *, invoiced=False, gl_posted=False, active=True):
    vendor = Vendor(name="Cloudhaven Hosting", default_expense_account="6300")
    session.add(vendor)
    session.flush()
    rv = RecurringVendor(
        vendor_id=vendor.id,
        expected_amount=2350.00,
        expected_day=3,
        account_code="6300",
        active=active,
    )
    session.add(rv)
    if invoiced:
        session.add(
            APInvoice(
                period=PERIOD,
                vendor_id=vendor.id,
                invoice_number="CH-30021",
                invoice_date="2026-01-03",
                amount=2350.00,
            )
        )
    if gl_posted:
        session.add(
            GLEntry(
                period=PERIOD,
                entry_date="2026-01-03",
                account_code="6300",
                description="Cloudhaven Hosting January service",
                debit=2350.00,
            )
        )
    session.commit()
    return vendor, rv


def test_rni_po_proposes_balanced_accrual():
    session = make_session()
    po, receipt = seed_rni_po(session)
    result = run_accruals(session, PERIOD)

    assert result.exceptions == []
    assert len(result.proposed_jes) == 1
    je = result.proposed_jes[0]
    assert je.rule == "rni_po"
    assert je.status == "auto_approved"
    debits = sum(line.debit for line in je.lines)
    credits = sum(line.credit for line in je.lines)
    assert abs(debits - credits) < 0.005
    assert abs(debits - 8420.75) < 0.005
    accounts = {line.account for line in je.lines}
    assert accounts == {"6100", ACCRUED_LIABILITIES_ACCOUNT}
    cited = {(ev.source_table, ev.row_id) for ev in je.evidence}
    assert ("goods_receipts", receipt.id) in cited
    assert ("purchase_orders", po.id) in cited
    assert "2026-02" in je.reason


def test_rni_po_silent_when_invoice_matches():
    session = make_session()
    seed_rni_po(session, invoiced=True)
    result = run_accruals(session, PERIOD)
    assert result.proposed_jes == []
    assert result.exceptions == []


def test_partial_receipt_becomes_exception_not_je():
    session = make_session()
    po, receipt = seed_rni_po(session, receipt_amount=3000.00)
    result = run_accruals(session, PERIOD)

    assert result.proposed_jes == []
    assert len(result.exceptions) == 1
    exc = result.exceptions[0]
    assert set(exc) == EXCEPTION_KEYS
    assert exc["category"] == "rni_po"
    assert exc["source_table"] == "goods_receipts"
    assert exc["row_id"] == receipt.id
    assert exc["period"] == PERIOD
    assert str(po.id) in exc["description"]


def test_missing_recurring_proposes_accrual_at_expected_amount():
    session = make_session()
    vendor, rv = seed_recurring(session)
    result = run_accruals(session, PERIOD)

    assert result.exceptions == []
    assert len(result.proposed_jes) == 1
    je = result.proposed_jes[0]
    assert je.rule == "missing_recurring"
    assert je.status == "needs_review"
    debits = sum(line.debit for line in je.lines)
    credits = sum(line.credit for line in je.lines)
    assert abs(debits - credits) < 0.005
    assert abs(debits - 2350.00) < 0.005
    cited = {(ev.source_table, ev.row_id) for ev in je.evidence}
    assert ("recurring_vendors", rv.id) in cited
    assert vendor.name in je.reason


def test_recurring_silent_when_invoiced():
    session = make_session()
    seed_recurring(session, invoiced=True)
    result = run_accruals(session, PERIOD)
    assert result.proposed_jes == []


def test_recurring_silent_when_gl_activity_present():
    session = make_session()
    seed_recurring(session, gl_posted=True)
    result = run_accruals(session, PERIOD)
    assert result.proposed_jes == []


def test_inactive_recurring_vendor_ignored():
    session = make_session()
    seed_recurring(session, active=False)
    result = run_accruals(session, PERIOD)
    assert result.proposed_jes == []


def test_policy_rule_auto_approves_missing_recurring():
    session = make_session()
    seed_recurring(session)
    rules = [
        PolicyRule(
            scope="accrual",
            condition="vendor Cloudhaven Hosting bill missing at close",
            action="accrue expected amount",
        )
    ]
    result = run_accruals(session, PERIOD, rules=rules)
    assert len(result.proposed_jes) == 1
    assert result.proposed_jes[0].status == "auto_approved"


def test_clean_data_yields_nothing():
    session = make_session()
    seed_rni_po(session, invoiced=True)
    seed_recurring(session, invoiced=True)
    result = run_accruals(session, PERIOD)
    assert result.proposed_jes == []
    assert result.exceptions == []
