from sqlalchemy.orm import Session

from sentinel.anomaly import run_anomaly
from sentinel.db import APInvoice, GLEntry, Vendor, get_engine, init_db
from sentinel.schemas import PolicyRule

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


def add_invoice(session, vendor, number, invoice_date, amount, period=PERIOD):
    invoice = APInvoice(
        period=period,
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


def test_vendor_name_variance_catches_corporate_suffix_variants():
    session = make_session()
    datadog = add_vendor(session, "Datadog")
    datadog_variant = add_vendor(session, "Datadog, Inc.")
    twilio = add_vendor(session, "Twilio")
    twilio_variant = add_vendor(session, "Twilio Inc.")
    add_invoice(session, datadog, "DD-4471", "2026-01-05", 2350.00)
    add_invoice(session, datadog_variant, "DD-4488", "2026-01-14", 2361.75)
    add_invoice(session, twilio, "TW-9052", "2026-01-07", 512.40)
    add_invoice(session, twilio_variant, "TW-9101", "2026-01-16", 498.15)

    result = run_anomaly(session, PERIOD)
    variances = by_category(result, "vendor_name_variance")
    pairs = {
        (exc["row_id"], partner)
        for exc in variances
        for partner in [int(exc["description"].split("vendors row ")[1].split(")")[0])]
    }
    assert pairs == {
        (datadog_variant.id, datadog.id),
        (twilio_variant.id, twilio.id),
    }


def test_vendor_name_variance_silent_on_distinct_vendors():
    session = make_session()
    gusto = add_vendor(session, "Gusto")
    datadog = add_vendor(session, "Datadog")
    add_invoice(session, gusto, "GUS-1201", "2026-01-15", 62410.22)
    add_invoice(session, datadog, "DD-4471", "2026-01-05", 2350.00)

    result = run_anomaly(session, PERIOD)
    assert by_category(result, "vendor_name_variance") == []


def test_vendor_name_variance_not_rereported_in_later_periods():
    session = make_session()
    datadog = add_vendor(session, "Datadog")
    variant = add_vendor(session, "Datadog, Inc.")
    add_invoice(session, datadog, "DD-4471", "2026-01-05", 2350.00)
    add_invoice(session, variant, "DD-4488", "2026-01-14", 2361.75)
    add_invoice(session, datadog, "DD-4532", "2026-02-05", 2344.10, period="2026-02")

    january = run_anomaly(session, PERIOD)
    assert len(by_category(january, "vendor_name_variance")) == 1

    february = run_anomaly(session, "2026-02")
    assert by_category(february, "vendor_name_variance") == []


def test_vendor_name_variance_rule_suppresses_covered_pair_only():
    session = make_session()
    datadog = add_vendor(session, "Datadog")
    datadog_variant = add_vendor(session, "Datadog, Inc.")
    twilio = add_vendor(session, "Twilio")
    twilio_variant = add_vendor(session, "Twilio Inc.")
    add_invoice(session, datadog, "DD-4471", "2026-01-05", 2350.00)
    add_invoice(session, datadog_variant, "DD-4488", "2026-01-14", 2361.75)
    add_invoice(session, twilio, "TW-9052", "2026-01-07", 512.40)
    add_invoice(session, twilio_variant, "TW-9101", "2026-01-16", 498.15)
    rule = PolicyRule(
        scope="anomaly:vendor_name_variance",
        condition="vendor:Datadog",
        action="suppress",
    )

    result = run_anomaly(session, PERIOD, rules=[rule])
    variances = by_category(result, "vendor_name_variance")
    assert len(variances) == 1
    assert variances[0]["row_id"] == twilio_variant.id


def test_vendor_name_variance_ignores_expired_or_inactive_rules():
    session = make_session()
    datadog = add_vendor(session, "Datadog")
    variant = add_vendor(session, "Datadog, Inc.")
    add_invoice(session, datadog, "DD-4471", "2026-01-05", 2350.00)
    add_invoice(session, variant, "DD-4488", "2026-01-14", 2361.75)
    expired = PolicyRule(
        scope="anomaly:vendor_name_variance",
        condition="vendor:Datadog",
        action="suppress",
        expires="2025-12",
    )
    inactive = PolicyRule(
        scope="anomaly:vendor_name_variance",
        condition="vendor:Datadog",
        action="suppress",
        active=False,
    )

    result = run_anomaly(session, PERIOD, rules=[expired, inactive])
    assert len(by_category(result, "vendor_name_variance")) == 1


def test_vendor_name_variance_distilled_suffix_rule_covers_every_suffix_pair():
    """A rule in the shape the distiller produces covers all suffix-only variants."""
    session = make_session()
    aws = add_vendor(session, "Amazon Web Services")
    aws_variant = add_vendor(session, "Amazon Web Services Inc")
    twilio = add_vendor(session, "Twilio")
    twilio_variant = add_vendor(session, "Twilio Inc.")
    figma = add_vendor(session, "Figma")
    figma_typo = add_vendor(session, "Figmaa")
    add_invoice(session, aws, "AWS-1", "2026-01-03", 8210.50)
    add_invoice(session, aws_variant, "AWS-2", "2026-01-12", 8194.10)
    add_invoice(session, twilio, "TW-1", "2026-01-07", 512.40)
    add_invoice(session, twilio_variant, "TW-2", "2026-01-16", 498.15)
    add_invoice(session, figma, "FG-1", "2026-01-08", 675.19)
    add_invoice(session, figma_typo, "FG-2", "2026-01-21", 675.19)
    rule = PolicyRule(
        scope=(
            "vendors table, vendor_name_variance category, matches against master "
            "vendor record 'Amazon Web Services' (partner vendors row 1)"
        ),
        condition=(
            "candidate vendor name equals the matched master vendor name after "
            "stripping a trailing legal-entity suffix (one of: Inc, Inc., LLC, Ltd) "
            "and similarity score >= 0.95"
        ),
        action="auto-approve the exception as a duplicate vendor record",
        limits={"suffix_whitelist": ["Inc", "Inc.", "LLC", "Ltd"], "min_similarity": 0.95},
    )

    result = run_anomaly(session, PERIOD, rules=[rule])
    variances = by_category(result, "vendor_name_variance")
    assert [item["row_id"] for item in variances] == [figma_typo.id]


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


def test_out_of_period_double_entry_yields_one_exception():
    session = make_session()
    debit = GLEntry(
        period=PERIOD,
        entry_date="2026-02-03",
        account_code="6600",
        description="Contractor services accrual",
        debit=1450.75,
        source="manual",
    )
    credit = GLEntry(
        period=PERIOD,
        entry_date="2026-02-03",
        account_code="2100",
        description="Contractor services accrual",
        credit=1450.75,
        source="manual",
    )
    other = GLEntry(
        period=PERIOD,
        entry_date="2026-02-05",
        account_code="6200",
        description="Late posted consulting fee",
        debit=875.40,
        source="manual",
    )
    session.add_all([debit, credit, other])
    session.commit()

    result = run_anomaly(session, PERIOD)
    out = by_category(result, "out_of_period")
    assert len(out) == 2
    assert {exc["row_id"] for exc in out} == {debit.id, other.id}


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
