"""Deterministic synthetic-data generator for the Ledger Sentinel demo company.

generate() rebuilds the SQLite database from scratch for the demo periods and
writes the planted-anomaly ground truth JSON. The same seed always produces
identical table contents and ground truth.

Ground truth conventions (the contract with the engines and the eval harness):

- One ground truth entry per anomalous row: category, source_table, row_id.
- duplicate_invoice points at the extra ap_invoices row, not the original.
- vendor_name_variance points at the ap_invoices row booked to the variant
  vendor name.
- round_number_split lists each split ap_invoices row separately.
- out_of_period lists each misdated gl_entries row separately.
- missing_recurring points at the recurring_vendors calendar row whose bill
  is absent this period.
- rni_po points at the purchase_orders row received but not invoiced.
- bank_fee and bank_interest point at bank-only bank_lines rows.
- timing_difference points at the gl_entries cash row whose bank line clears
  in the next period (or is still outstanding after the last period).
"""

from __future__ import annotations

import calendar
import json
import os
import random
from datetime import date, timedelta

from sqlalchemy.orm import Session

from sentinel.datagen import company
from sentinel.db import (
    Account,
    APInvoice,
    BankLine,
    DodoPayout,
    GLEntry,
    GoodsReceipt,
    PurchaseOrder,
    RecurringVendor,
    Vendor,
    get_engine,
    get_session,
    init_db,
)

DEFAULT_DB_PATH = "data/sentinel.db"
DEFAULT_GROUND_TRUTH_PATH = "data/ground_truth.json"

ANOMALY_CATEGORIES = (
    "duplicate_invoice",
    "vendor_name_variance",
    "round_number_split",
    "out_of_period",
    "missing_recurring",
    "rni_po",
    "bank_fee",
    "bank_interest",
    "timing_difference",
)

_TERMS_DAYS = {"due_on_receipt": 0, "net5": 5, "net15": 15, "net30": 30}
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _ref(rng: random.Random) -> str:
    return "".join(rng.choice(_REF_ALPHABET) for _ in range(5))


def _jitter(rng: random.Random, base: float, frac: float) -> float:
    if frac <= 0:
        return round(base, 2)
    return round(base * (1 + rng.uniform(-frac, frac)), 2)


def _month_day(period: str, day: int) -> date:
    year, month = (int(part) for part in period.split("-"))
    last = calendar.monthrange(year, month)[1]
    return date(year, month, max(1, min(day, last)))


def _last_day(period: str) -> date:
    return _month_day(period, 31)


def _next_period(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


class _Generator:
    def __init__(self, session: Session, rng: random.Random) -> None:
        self.session = session
        self.rng = rng
        self.ground_truth: dict[str, list[dict]] = {p: [] for p in company.PERIODS}
        self.bank_matches: dict[str, list[dict]] = {p: [] for p in company.PERIODS}
        self.vendors: dict[str, Vendor] = {}
        self.recurring_rows: dict[str, RecurringVendor] = {}
        self.carried_bank_lines: list[dict] = []
        self.carried_invoices: list[dict] = []

    def run(self) -> None:
        self._insert_accounts()
        self._insert_vendors()
        self._insert_recurring_calendar()
        for period in company.PERIODS:
            self._generate_month(period)

    def _add(self, row):
        self.session.add(row)
        self.session.flush()
        return row

    def _gl(
        self,
        period: str,
        on: date,
        account: str,
        description: str,
        *,
        debit: float = 0.0,
        credit: float = 0.0,
        source: str | None = None,
    ) -> GLEntry:
        return self._add(
            GLEntry(
                period=period,
                entry_date=on.isoformat(),
                account_code=account,
                description=description,
                debit=debit,
                credit=credit,
                source=source,
            )
        )

    def _bank(
        self,
        period: str,
        on: date,
        descriptor: str,
        amount: float,
        reference: str | None = None,
    ) -> BankLine:
        return self._add(
            BankLine(
                period=period,
                line_date=on.isoformat(),
                descriptor=descriptor,
                amount=amount,
                reference=reference,
            )
        )

    def _plant(
        self, period: str, category: str, source_table: str, row_id: int, description: str
    ) -> None:
        self.ground_truth[period].append(
            {
                "category": category,
                "source_table": source_table,
                "row_id": row_id,
                "description": description,
            }
        )

    def _insert_accounts(self) -> None:
        for code, name, kind in company.ACCOUNTS:
            self._add(Account(code=code, name=name, type=kind))

    def _insert_vendors(self) -> None:
        for name, (_code, terms, account, _prefix) in company.VENDORS.items():
            self.vendors[name] = self._add(
                Vendor(name=name, payment_terms=terms, default_expense_account=account)
            )

    def _insert_recurring_calendar(self) -> None:
        for name, (amount, day) in company.RECURRING.items():
            account = company.VENDORS[name][2]
            self.recurring_rows[name] = self._add(
                RecurringVendor(
                    vendor_id=self.vendors[name].id,
                    expected_amount=amount,
                    expected_day=day,
                    account_code=account,
                    active=True,
                )
            )

    def _invoice(
        self,
        period: str,
        vendor_row: Vendor,
        expense_account: str,
        code: str,
        amount: float,
        on: date,
        *,
        number: str | None = None,
    ) -> APInvoice:
        if number is None:
            number = f"INV-{code}-{period[2:4]}{period[5:7]}{self.rng.randint(100, 999)}"
        terms = vendor_row.payment_terms or "net15"
        due = on + timedelta(days=_TERMS_DAYS.get(terms, 15))
        invoice = self._add(
            APInvoice(
                period=period,
                vendor_id=vendor_row.id,
                invoice_number=number,
                invoice_date=on.isoformat(),
                due_date=due.isoformat(),
                amount=amount,
                status="open",
            )
        )
        text = f"{vendor_row.name} invoice {number}"
        self._gl(period, on, expense_account, text, debit=amount, source="ap")
        self._gl(period, on, company.AP, text, credit=amount, source="ap")
        return invoice

    def _vendor_invoice(self, period: str, name: str, amount: float, day: int) -> APInvoice:
        code, _terms, account, _prefix = company.VENDORS[name]
        return self._invoice(
            period, self.vendors[name], account, code, amount, _month_day(period, day)
        )

    def _pay_gl(self, period: str, vendor_name: str, invoice: APInvoice, on: date) -> GLEntry:
        text = f"Payment to {vendor_name} {invoice.invoice_number}"
        self._gl(period, on, company.AP, text, debit=invoice.amount, source="ap_payment")
        cash = self._gl(period, on, company.CASH, text, credit=invoice.amount, source="ap_payment")
        invoice.status = "paid"
        return cash

    def _pay(
        self,
        period: str,
        vendor_name: str,
        invoice: APInvoice,
        on: date,
        *,
        bank_offset: int = 0,
        carry: bool = False,
    ) -> tuple[GLEntry, BankLine | None]:
        cash = self._pay_gl(period, vendor_name, invoice, on)
        prefix = company.VENDORS[vendor_name][3]
        descriptor = f"{prefix} {_ref(self.rng)}"
        if carry:
            nxt = _next_period(period)
            self.carried_bank_lines.append(
                {
                    "period": nxt,
                    "date": _month_day(nxt, self.rng.randint(1, 3)),
                    "descriptor": descriptor,
                    "amount": -invoice.amount,
                }
            )
            return cash, None
        line = self._bank(period, on + timedelta(days=bank_offset), descriptor, -invoice.amount)
        self.bank_matches[period].append({"bank_line_id": line.id, "gl_ids": [cash.id]})
        return cash, line

    def _generate_month(self, period: str) -> None:
        rng = self.rng
        cast = company.CASTING[period]
        last = _last_day(period)
        month_invoices: dict[str, APInvoice] = {}

        # bank lines carried in from the prior month's timing difference
        for spec in self.carried_bank_lines:
            if spec["period"] == period:
                self._bank(period, spec["date"], spec["descriptor"], spec["amount"])
        self.carried_bank_lines = [s for s in self.carried_bank_lines if s["period"] != period]

        # invoices arriving for the prior month's received-not-invoiced POs
        for spec in self.carried_invoices:
            invoice = self._vendor_invoice(
                period, spec["vendor"], spec["amount"], rng.randint(3, 6)
            )
            self._pay(period, spec["vendor"], invoice, _month_day(period, rng.randint(8, 12)))
        self.carried_invoices = []

        # recurring bills, one of which is planted as absent
        for name, (expected, day) in company.RECURRING.items():
            if name == cast["missing_recurring"]:
                row = self.recurring_rows[name]
                self._plant(
                    period,
                    "missing_recurring",
                    "recurring_vendors",
                    row.id,
                    f"Expected recurring bill from {name} (about {expected:.2f}, "
                    f"usually day {day}) is absent in {period}",
                )
                continue
            amount = _jitter(rng, expected, company.RECURRING_JITTER[name])
            invoice = self._vendor_invoice(period, name, amount, day)
            wait = 0 if company.VENDORS[name][1] == "due_on_receipt" else rng.randint(2, 6)
            pay_on = min(_month_day(period, day + wait), last)
            offset = company.FUZZY_BANK_OFFSET.get(name, 0)
            self._pay(period, name, invoice, pay_on, bank_offset=offset)
            month_invoices.setdefault(name, invoice)

        # second payroll run at the end of the month
        amount = _jitter(rng, company.RECURRING["Gusto"][0], 0.03)
        run_day = last.day - 2
        invoice = self._vendor_invoice(period, "Gusto", amount, run_day)
        self._pay(period, "Gusto", invoice, _month_day(period, run_day))

        # one-off vendor bills, one of which clears the bank only next month
        for name, (base, jit) in company.ONE_OFF_BILLS.items():
            amount = _jitter(rng, base, jit)
            if name == cast["timing_vendor"]:
                invoice = self._vendor_invoice(period, name, amount, rng.randint(20, 23))
                cash, _line = self._pay(
                    period, name, invoice, _month_day(period, rng.randint(26, 28)), carry=True
                )
                self._plant(
                    period,
                    "timing_difference",
                    "gl_entries",
                    cash.id,
                    f"Payment to {name} posted to cash on {cash.entry_date} but the "
                    f"bank line clears after {period} month end",
                )
            else:
                invoice = self._vendor_invoice(period, name, amount, rng.randint(4, 16))
                inv_day = date.fromisoformat(invoice.invoice_date).day
                pay_on = min(_month_day(period, inv_day + rng.randint(2, 6)), last)
                self._pay(period, name, invoice, pay_on)
            month_invoices.setdefault(name, invoice)

        # bills recorded late in the month and still unpaid at month end
        for name, (base, jit) in company.OPEN_INVOICE_BILLS.items():
            amount = _jitter(rng, base, jit)
            invoice = self._vendor_invoice(period, name, amount, rng.randint(23, 26))
            month_invoices.setdefault(name, invoice)

        # several invoices from one vendor settled by a single bank transfer
        group_vendor, group_size = cast["one_to_many"]
        invoices = [
            self._vendor_invoice(
                period, group_vendor, round(rng.uniform(240.0, 980.0), 2), rng.randint(5, 15)
            )
            for _ in range(group_size)
        ]
        pay_on = _month_day(period, rng.randint(17, 21))
        cash_ids = [self._pay_gl(period, group_vendor, inv, pay_on).id for inv in invoices]
        total = round(sum(inv.amount for inv in invoices), 2)
        prefix = company.VENDORS[group_vendor][3]
        line = self._bank(period, pay_on, f"{prefix} BATCH {_ref(rng)}", -total)
        self.bank_matches[period].append({"bank_line_id": line.id, "gl_ids": cash_ids})
        month_invoices.setdefault(group_vendor, invoices[0])

        self._dodo_payouts(period)
        self._purchase_orders(period, cast)
        self._bank_only_lines(period, last)
        self._planted_ap_anomalies(period, cast, month_invoices)
        self._out_of_period(period, cast)

    def _dodo_payouts(self, period: str) -> None:
        rng = self.rng
        for n in range(company.DODO_PAYOUTS_PER_MONTH):
            payout_on = _month_day(period, 7 * (n + 1) + rng.randint(-1, 1))
            gross = round(rng.uniform(28000.0, 45000.0), 2)
            fee = round(gross * company.DODO_FEE_RATE, 2)
            net = round(gross - fee, 2)
            reference = f"DP-{period}-{n + 1}"
            self._add(
                DodoPayout(
                    period=period,
                    payout_date=payout_on.isoformat(),
                    gross_amount=gross,
                    fee_amount=fee,
                    net_amount=net,
                    reference=reference,
                )
            )
            text = f"Dodo Payments payout {reference}"
            cash = self._gl(period, payout_on, company.CASH, text, debit=net, source="dodo")
            self._gl(period, payout_on, company.FEES_EXPENSE, text, debit=fee, source="dodo")
            self._gl(period, payout_on, company.REVENUE, text, credit=gross, source="dodo")
            deposit_on = payout_on + timedelta(days=1) if n == 1 else payout_on
            line = self._bank(
                period, deposit_on, f"DODO PAYMENTS {_ref(rng)}", net, reference=reference
            )
            self.bank_matches[period].append({"bank_line_id": line.id, "gl_ids": [cash.id]})

    def _purchase_orders(self, period: str, cast: dict) -> None:
        rng = self.rng
        tag = f"{period[2:4]}{period[5:7]}"
        for n, name in enumerate(("Uline", "Staples"), start=1):
            amount = round(rng.uniform(900.0, 4200.0), 2)
            order_on = _month_day(period, rng.randint(3, 8))
            po = self._add(
                PurchaseOrder(
                    period=period,
                    vendor_id=self.vendors[name].id,
                    po_number=f"PO-{tag}-{n}",
                    order_date=order_on.isoformat(),
                    amount=amount,
                    status="closed",
                )
            )
            receipt_on = _month_day(period, rng.randint(12, 18))
            self._add(
                GoodsReceipt(
                    period=period, po_id=po.id, receipt_date=receipt_on.isoformat(), amount=amount
                )
            )
            code, _terms, account, _prefix = company.VENDORS[name]
            invoice = self._invoice(
                period, self.vendors[name], account, code, amount, receipt_on + timedelta(days=2)
            )
            self._pay(period, name, invoice, receipt_on + timedelta(days=5))

        # one PO received near month end with no invoice yet
        name = cast["rni_vendor"]
        amount = round(rng.uniform(1500.0, 6000.0), 2)
        order_on = _month_day(period, rng.randint(14, 17))
        po = self._add(
            PurchaseOrder(
                period=period,
                vendor_id=self.vendors[name].id,
                po_number=f"PO-{tag}-3",
                order_date=order_on.isoformat(),
                amount=amount,
                status="received",
            )
        )
        receipt_on = _month_day(period, rng.randint(24, 27))
        self._add(
            GoodsReceipt(
                period=period, po_id=po.id, receipt_date=receipt_on.isoformat(), amount=amount
            )
        )
        self._plant(
            period,
            "rni_po",
            "purchase_orders",
            po.id,
            f"PO {po.po_number} from {name} received {receipt_on.isoformat()} "
            f"but not invoiced by {period} month end",
        )
        self.carried_invoices.append({"vendor": name, "amount": amount})

    def _bank_only_lines(self, period: str, last: date) -> None:
        rng = self.rng
        fee = self._bank(period, last, "MONTHLY SERVICE FEE", -round(rng.uniform(38.0, 55.0), 2))
        self._plant(
            period,
            "bank_fee",
            "bank_lines",
            fee.id,
            "Bank service fee on the statement with no GL entry",
        )
        interest = self._bank(period, last, "INTEREST CREDIT", round(rng.uniform(14.0, 42.0), 2))
        self._plant(
            period,
            "bank_interest",
            "bank_lines",
            interest.id,
            "Interest credit on the statement with no GL entry",
        )

    def _planted_ap_anomalies(
        self, period: str, cast: dict, month_invoices: dict[str, APInvoice]
    ) -> None:
        rng = self.rng

        # the same bill keyed a second time a few days later
        original = month_invoices[cast["duplicate_vendor"]]
        vendor = self.session.get(Vendor, original.vendor_id)
        code, _terms, account, _prefix = company.VENDORS[vendor.name]
        dup_on = min(
            date.fromisoformat(original.invoice_date) + timedelta(days=rng.randint(1, 4)),
            _last_day(period),
        )
        duplicate = self._invoice(
            period,
            vendor,
            account,
            code,
            original.amount,
            dup_on,
            number=f"{original.invoice_number}A",
        )
        self._plant(
            period,
            "duplicate_invoice",
            "ap_invoices",
            duplicate.id,
            f"Invoice {original.invoice_number} from {vendor.name} keyed twice; "
            f"{duplicate.invoice_number} duplicates amount {original.amount:.2f}",
        )

        # invoice booked to a variant of an existing vendor name
        base_name, variant_name = cast["vendor_variant"]
        base = company.VENDORS[base_name]
        variant = self._add(
            Vendor(name=variant_name, payment_terms=base[1], default_expense_account=base[2])
        )
        if base_name in company.RECURRING:
            amount = _jitter(rng, company.RECURRING[base_name][0], 0.05)
        elif base_name in company.ONE_OFF_BILLS:
            amount = _jitter(rng, *company.ONE_OFF_BILLS[base_name])
        else:
            amount = round(rng.uniform(400.0, 1600.0), 2)
        variant_invoice = self._invoice(
            period, variant, base[2], base[0], amount, _month_day(period, rng.randint(12, 20))
        )
        self._plant(
            period,
            "vendor_name_variance",
            "ap_invoices",
            variant_invoice.id,
            f"Invoice {variant_invoice.invoice_number} booked to '{variant_name}' "
            f"while the vendor master has '{base_name}'",
        )

        # one engagement split into two invoices under the approval limit
        split_name = cast["split_vendor"]
        first_day = rng.randint(15, 20)
        for k in range(2):
            amount = round(rng.uniform(4550.0, 4985.0), 2)
            invoice = self._vendor_invoice(period, split_name, amount, first_day + 2 * k)
            self._plant(
                period,
                "round_number_split",
                "ap_invoices",
                invoice.id,
                f"Split invoice {k + 1} of 2 from {split_name}, each just under "
                f"the {company.APPROVAL_LIMIT:.2f} approval limit",
            )

    def _out_of_period(self, period: str, cast: dict) -> None:
        wrong = date.fromisoformat(cast["out_of_period_date"])
        amount = round(self.rng.uniform(700.0, 2400.0), 2)
        text = "Contractor services accrual"
        debit_row = self._gl(period, wrong, "6600", text, debit=amount, source="manual")
        credit_row = self._gl(period, wrong, company.ACCRUED, text, credit=amount, source="manual")
        for row in (debit_row, credit_row):
            self._plant(
                period,
                "out_of_period",
                "gl_entries",
                row.id,
                f"GL entry dated {wrong.isoformat()} posted to period {period}",
            )


def generate(
    db_path: str = DEFAULT_DB_PATH,
    seed: int = 42,
    ground_truth_path: str = DEFAULT_GROUND_TRUTH_PATH,
) -> dict:
    """Rebuild the database at db_path and write the ground truth JSON.

    Returns a summary with the ground truth, the clean bank-to-GL match list
    per period (for tests), and headline row counts.
    """
    for path in (db_path, ground_truth_path):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)

    engine = get_engine(db_path)
    init_db(engine)
    rng = random.Random(seed)
    with get_session(engine) as session:
        builder = _Generator(session, rng)
        builder.run()
        counts = {
            "vendors": session.query(Vendor).count(),
            "gl_entries": session.query(GLEntry).count(),
            "bank_lines": session.query(BankLine).count(),
            "ap_invoices": session.query(APInvoice).count(),
            "purchase_orders": session.query(PurchaseOrder).count(),
            "dodo_payouts": session.query(DodoPayout).count(),
        }
    engine.dispose()

    with open(ground_truth_path, "w", encoding="utf-8") as handle:
        json.dump(builder.ground_truth, handle, indent=2)
        handle.write("\n")

    return {
        "db_path": db_path,
        "seed": seed,
        "ground_truth_path": ground_truth_path,
        "ground_truth": builder.ground_truth,
        "bank_matches": builder.bank_matches,
        "counts": counts,
    }
