import json
from datetime import date

import pytest

from sentinel import db as sdb
from sentinel.datagen import ANOMALY_CATEGORIES, company, generate

TABLES = {
    "accounts": sdb.Account,
    "gl_entries": sdb.GLEntry,
    "bank_lines": sdb.BankLine,
    "vendors": sdb.Vendor,
    "ap_invoices": sdb.APInvoice,
    "purchase_orders": sdb.PurchaseOrder,
    "goods_receipts": sdb.GoodsReceipt,
    "recurring_vendors": sdb.RecurringVendor,
    "dodo_payouts": sdb.DodoPayout,
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("datagen")
    db_path = str(root / "sentinel.db")
    summary = generate(db_path, seed=42, ground_truth_path=str(root / "ground_truth.json"))
    engine = sdb.get_engine(db_path)
    yield engine, summary
    engine.dispose()


def _dump(db_path):
    engine = sdb.get_engine(db_path)
    try:
        with sdb.get_session(engine) as session:
            out = {}
            for name, cls in TABLES.items():
                rows = session.query(cls).order_by(cls.id).all()
                out[name] = [
                    tuple(getattr(row, column.name) for column in cls.__table__.columns)
                    for row in rows
                ]
            return out
    finally:
        engine.dispose()


def test_same_seed_produces_identical_output(tmp_path):
    first = generate(str(tmp_path / "a.db"), seed=7, ground_truth_path=str(tmp_path / "a.json"))
    second = generate(str(tmp_path / "b.db"), seed=7, ground_truth_path=str(tmp_path / "b.json"))
    assert _dump(str(tmp_path / "a.db")) == _dump(str(tmp_path / "b.db"))
    assert first["ground_truth"] == second["ground_truth"]
    a_file = json.loads((tmp_path / "a.json").read_text())
    b_file = json.loads((tmp_path / "b.json").read_text())
    assert a_file == b_file == first["ground_truth"]


def test_row_counts_in_expected_ranges(generated):
    engine, _summary = generated
    with sdb.get_session(engine) as session:
        assert session.query(sdb.Account).count() == len(company.ACCOUNTS)
        assert 15 <= session.query(sdb.Vendor).count() <= 25
        assert session.query(sdb.RecurringVendor).count() == len(company.RECURRING)
        for period in company.PERIODS:
            assert 60 <= session.query(sdb.GLEntry).filter_by(period=period).count() <= 140
            assert 15 <= session.query(sdb.BankLine).filter_by(period=period).count() <= 40
            assert 15 <= session.query(sdb.APInvoice).filter_by(period=period).count() <= 32
            assert session.query(sdb.DodoPayout).filter_by(period=period).count() == 4
            assert session.query(sdb.PurchaseOrder).filter_by(period=period).count() == 3
            assert session.query(sdb.GoodsReceipt).filter_by(period=period).count() == 3


def test_ground_truth_rows_exist(generated):
    engine, summary = generated
    ground_truth = summary["ground_truth"]
    assert set(ground_truth) == set(company.PERIODS)
    with sdb.get_session(engine) as session:
        for entries in ground_truth.values():
            assert {entry["category"] for entry in entries} == set(ANOMALY_CATEGORIES)
            for entry in entries:
                row = session.get(TABLES[entry["source_table"]], entry["row_id"])
                assert row is not None, entry
                assert entry["description"]


def test_out_of_period_ground_truth_is_one_entry_per_posting(generated):
    engine, summary = generated
    with sdb.get_session(engine) as session:
        for entries in summary["ground_truth"].values():
            planted = [e for e in entries if e["category"] == "out_of_period"]
            assert len(planted) == 1
            entry = planted[0]
            assert entry["source_table"] == "gl_entries"
            row = session.get(sdb.GLEntry, entry["row_id"])
            assert row.entry_date[:7] != row.period
            legs = (
                session.query(sdb.GLEntry)
                .filter_by(
                    entry_date=row.entry_date,
                    description=row.description,
                    source=row.source,
                )
                .all()
            )
            assert len(legs) == 2, "misdated posting keeps both GL legs"
            assert row.id == min(leg.id for leg in legs)


def test_planted_duplicates_are_near_duplicates(generated):
    engine, summary = generated
    with sdb.get_session(engine) as session:
        for period, entries in summary["ground_truth"].items():
            dups = [e for e in entries if e["category"] == "duplicate_invoice"]
            assert dups
            for entry in dups:
                dup = session.get(sdb.APInvoice, entry["row_id"])
                siblings = (
                    session.query(sdb.APInvoice)
                    .filter_by(period=period, vendor_id=dup.vendor_id)
                    .all()
                )
                twins = [
                    row
                    for row in siblings
                    if row.id != dup.id
                    and abs(row.amount - dup.amount) < 0.005
                    and abs(
                        (
                            date.fromisoformat(row.invoice_date)
                            - date.fromisoformat(dup.invoice_date)
                        ).days
                    )
                    <= 5
                ]
                assert twins, "planted duplicate must shadow a real invoice"


def test_clean_bank_lines_match_gl_cash(generated):
    engine, summary = generated
    with sdb.get_session(engine) as session:
        for period in company.PERIODS:
            matches = summary["bank_matches"][period]
            assert matches
            assert any(len(m["gl_ids"]) > 1 for m in matches), "need a one-to-many match"
            for match in matches:
                line = session.get(sdb.BankLine, match["bank_line_id"])
                gl_rows = [session.get(sdb.GLEntry, gid) for gid in match["gl_ids"]]
                net = sum(row.debit - row.credit for row in gl_rows)
                assert abs(line.amount - net) < 0.005
                line_date = date.fromisoformat(line.line_date)
                for row in gl_rows:
                    assert row.account_code == company.CASH
                    assert row.period == period
                    assert abs((line_date - date.fromisoformat(row.entry_date)).days) <= 3


def test_cli_module_entry(tmp_path):
    from sentinel.datagen.__main__ import main

    main(
        [
            "--db",
            str(tmp_path / "cli.db"),
            "--seed",
            "3",
            "--ground-truth",
            str(tmp_path / "cli.json"),
        ]
    )
    assert (tmp_path / "cli.db").exists()
    data = json.loads((tmp_path / "cli.json").read_text())
    assert set(data) == set(company.PERIODS)
