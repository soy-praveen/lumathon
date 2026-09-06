from sqlalchemy.orm import Session

from sentinel.anomaly import run_anomaly
from sentinel.db import APInvoice, GLEntry, Vendor, get_engine, init_db

PERIOD = "2026-01"
EXCEPTION_KEYS = {"category", "description", "source_table", "row_id", "period"}


def make_session() -> Session:
    engine = get_engine(":memory:")
    init_db(engine)
    return Session(engine)


def add_vendor(session, name):
    vendor = Vendor(name=name)
    session.add(vendor)
    session.commit()
    return vendor


def add_invoice(session, vendor, number, invoice_date, amount):
    invoice = APInvoice(
        period=PERIOD,
        vendor_id=vendor.id,
        invoice_number=number,
        invoice_date=invoice_date,
        amount=amount,
    )
    session.add(invoice)
    session.commit()
    return invoice


def by_category(result, category):
    return [exc for exc in result.exceptions if exc["category"] == category]


def test_duplicate_invoice_fires_on_near_same_pair():
    session = make_session()
    vendor = add_vendor(session, "Stellar Office Supply")
    first = add_invoice(session, vendor, "SOS-1187", "2026-01-08", 1240.50)
    second = add_invoice(session, vendor, "SOS-1204", "2026-01-11", 1240.50)

    result = run_anomaly(session, PERIOD)
    dupes = by_category(result, "duplicate_invoice")
    assert len(dupes) == 1
    exc = dupes[0]
    assert set(exc) == EXCEPTION_KEYS
    assert exc["source_table"] == "ap_invoices"
    assert exc["row_id"] == second.id
    assert str(first.id) in exc["description"]
    assert exc["period"] == PERIOD


def test_duplicate_invoice_silent_on_different_amounts():
    session = make_session()
    vendor = add_vendor(session, "Stellar Office Supply")
    add_invoice(session, vendor, "SOS-1187", "2026-01-08", 1240.50)
    add_invoice(session, vendor, "SOS-1204", "2026-01-11", 3610.25)

    result = run_anomaly(session, PERIOD)
    assert by_category(result, "duplicate_invoice") == []


def test_vendor_name_variance_fires_on_near_duplicate_names():
    session = make_session()
    first = add_vendor(session, "Northwind Traders LLC")
    second = add_vendor(session, "Northwind Traders")

    result = run_anomaly(session, PERIOD)
    variances = by_category(result, "vendor_name_variance")
    assert len(variances) == 1
    exc = variances[0]
    assert exc["source_table"] == "vendors"
    assert exc["row_id"] == second.id
    assert str(first.id) in exc["description"]


def test_round_number_split_fires_on_invoices_just_under_limit():
    session = make_session()
    vendor = add_vendor(session, "Apex Building Services")
    first = add_invoice(session, vendor, "ABS-2201", "2026-01-12", 4890.00)
    second = add_invoice(session, vendor, "ABS-2214", "2026-01-15", 4960.00)

    result = run_anomaly(session, PERIOD)
    splits = by_category(result, "round_number_split")
    assert len(splits) == 1
    exc = splits[0]
    assert exc["source_table"] == "ap_invoices"
    assert exc["row_id"] == first.id
    assert str(second.id) in exc["description"]


def test_round_number_split_fires_on_suspiciously_round_amount():
    session = make_session()
    vendor = add_vendor(session, "Meridian Consulting Group")
    invoice = add_invoice(session, vendor, "MCG-0455", "2026-01-20", 12000.00)

    result = run_anomaly(session, PERIOD)
    splits = by_category(result, "round_number_split")
    assert len(splits) == 1
    assert splits[0]["row_id"] == invoice.id


def test_out_of_period_fires_on_mismatched_entry_date():
    session = make_session()
    entry = GLEntry(
        period=PERIOD,
        entry_date="2026-02-03",
        account_code="6200",
        description="Late posted consulting fee",
        debit=875.40,
    )
    session.add(entry)
    session.commit()

    result = run_anomaly(session, PERIOD)
    out = by_category(result, "out_of_period")
    assert len(out) == 1
    exc = out[0]
    assert exc["source_table"] == "gl_entries"
    assert exc["row_id"] == entry.id
    assert "2026-02-03" in exc["description"]


def test_clean_data_stays_silent():
    session = make_session()
    initech = add_vendor(session, "Initech Solutions")
    vandelay = add_vendor(session, "Vandelay Imports")
    add_invoice(session, initech, "INI-7731", "2026-01-09", 1287.53)
    add_invoice(session, vandelay, "VAN-0092", "2026-01-17", 2760.10)
    session.add(
        GLEntry(
            period=PERIOD,
            entry_date="2026-01-17",
            account_code="6200",
            description="Vandelay Imports freight",
            debit=2760.10,
        )
    )
    session.commit()

    result = run_anomaly(session, PERIOD)
    assert result.exceptions == []
