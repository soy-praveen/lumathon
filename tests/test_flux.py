from sqlalchemy.orm import Session

from sentinel.db import GLEntry, get_engine, init_db
from sentinel.flux import run_flux

PERIOD = "2026-02"
PRIOR = "2026-01"
EXCEPTION_KEYS = {"category", "description", "source_table", "row_id", "period"}
MOVEMENT_KEYS = {
    "account_code",
    "current",
    "prior",
    "delta",
    "pct",
    "explained",
    "explanation",
    "evidence",
}


def make_session() -> Session:
    engine = get_engine(":memory:")
    init_db(engine)
    return Session(engine)


def add_gl(session, period, account, description, debit=0.0, credit=0.0, entry_date=None):
    entry = GLEntry(
        period=period,
        entry_date=entry_date or f"{period}-15",
        account_code=account,
        description=description,
        debit=debit,
        credit=credit,
    )
    session.add(entry)
    session.commit()
    return entry


def test_flags_and_explains_movement_from_one_off_entry():
    session = make_session()
    add_gl(session, PRIOR, "6200", "Initech Solutions retainer", debit=9500.00)
    add_gl(session, PERIOD, "6200", "Initech Solutions retainer", debit=9500.00)
    driver = add_gl(session, PERIOD, "6200", "Vandelay Imports warehouse buildout", debit=21375.40)

    result = run_flux(session, PERIOD, PRIOR)

    assert len(result.movements) == 1
    movement = result.movements[0]
    assert set(movement) == MOVEMENT_KEYS
    assert movement["account_code"] == "6200"
    assert abs(movement["delta"] - 21375.40) < 0.005
    assert movement["pct"] is not None and movement["pct"] > 200
    assert movement["explained"] is True
    assert {"source_table": "gl_entries", "row_id": driver.id} in movement["evidence"]
    assert result.exceptions == []


def test_movement_below_absolute_threshold_not_flagged():
    session = make_session()
    add_gl(session, PRIOR, "6400", "Metro courier services", debit=1200.00)
    add_gl(session, PERIOD, "6400", "Metro courier services", debit=1200.00)
    add_gl(session, PERIOD, "6400", "Rush delivery surcharge", debit=4200.00)

    result = run_flux(session, PERIOD, PRIOR)
    assert result.movements == []
    assert result.exceptions == []


def test_movement_below_percent_threshold_not_flagged():
    session = make_session()
    add_gl(session, PRIOR, "5000", "Payroll run January", debit=100000.00)
    add_gl(session, PERIOD, "5000", "Payroll run February", debit=106000.00)

    result = run_flux(session, PERIOD, PRIOR)
    assert result.movements == []
    assert result.exceptions == []


def test_unexplained_movement_raises_exception():
    session = make_session()
    add_gl(session, PRIOR, "6500", "Office supplies", debit=1000.00)
    add_gl(session, PERIOD, "6500", "Office supplies", debit=1000.00)
    for i in range(8):
        add_gl(session, PERIOD, "6500", f"Misc purchase batch {i + 1}", debit=1500.00)

    result = run_flux(session, PERIOD, PRIOR)

    assert len(result.movements) == 1
    movement = result.movements[0]
    assert movement["explained"] is False
    assert len(result.exceptions) == 1
    exc = result.exceptions[0]
    assert set(exc) == EXCEPTION_KEYS
    assert exc["category"] == "flux_unexplained"
    assert exc["source_table"] == "gl_entries"
    assert exc["row_id"] is not None
    assert exc["period"] == PERIOD


def test_custom_thresholds_respected():
    session = make_session()
    add_gl(session, PRIOR, "6600", "Facilities maintenance", debit=50000.00)
    add_gl(session, PERIOD, "6600", "Facilities maintenance", debit=50000.00)
    add_gl(session, PERIOD, "6600", "Roof repair one-time", debit=6000.00)

    default_result = run_flux(session, PERIOD, PRIOR)
    assert len(default_result.movements) == 1

    higher_abs = run_flux(session, PERIOD, PRIOR, threshold_abs=7000.0)
    assert higher_abs.movements == []

    higher_pct = run_flux(session, PERIOD, PRIOR, threshold_pct=15.0)
    assert higher_pct.movements == []


def test_stable_accounts_produce_no_output():
    session = make_session()
    add_gl(session, PRIOR, "6700", "Harbor Point rent", debit=14250.00)
    add_gl(session, PERIOD, "6700", "Harbor Point rent", debit=14250.00)

    result = run_flux(session, PERIOD, PRIOR)
    assert result.movements == []
    assert result.exceptions == []
