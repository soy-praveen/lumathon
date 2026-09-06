from sqlalchemy import inspect

from sentinel.db import get_engine, get_session, init_db

EXPECTED_TABLES = {
    "accounts",
    "gl_entries",
    "bank_lines",
    "vendors",
    "ap_invoices",
    "purchase_orders",
    "goods_receipts",
    "recurring_vendors",
    "dodo_payouts",
    "proposed_jes",
    "je_lines",
    "exceptions",
    "policy_rules",
    "audit_log",
}


def test_init_db_creates_all_tables():
    engine = get_engine(":memory:")
    init_db(engine)
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_get_engine_reads_env_when_path_is_none(monkeypatch, tmp_path):
    db_path = tmp_path / "env-test.db"
    monkeypatch.setenv("SENTINEL_DB", str(db_path))
    engine = get_engine()
    assert engine.url.database == str(db_path)


def test_get_session_commits(tmp_path):
    from sentinel.db import Account

    engine = get_engine(str(tmp_path / "session-test.db"))
    init_db(engine)
    with get_session(engine) as session:
        session.add(Account(code="1000", name="Cash - Operating", type="asset"))
    with get_session(engine) as session:
        account = session.query(Account).filter_by(code="1000").one()
        assert account.name == "Cash - Operating"
