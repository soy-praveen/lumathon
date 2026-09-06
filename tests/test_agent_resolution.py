import pytest

from sentinel.agent.resolution import resolve_exception
from sentinel.db import AuditLog, ExceptionRecord, get_engine, get_session, init_db


def make_engine_with_exception(tmp_path):
    engine = get_engine(str(tmp_path / "resolution-test.db"))
    init_db(engine)
    with get_session(engine) as session:
        record = ExceptionRecord(
            period="2026-02",
            category="recon",
            description="unmatched bank line CHECK 1088 - 4200.00",
            source_table="bank_lines",
            row_id=19,
        )
        session.add(record)
        session.flush()
        exception_id = record.id
    return engine, exception_id


def test_resolve_marks_resolved_and_writes_audit(tmp_path):
    engine, exception_id = make_engine_with_exception(tmp_path)
    result = resolve_exception(
        engine, exception_id, "approve", "verified against the check register", "maria"
    )
    assert result == {
        "id": exception_id,
        "status": "resolved",
        "resolution": "approve",
        "resolution_reason": "verified against the check register",
    }
    with get_session(engine) as session:
        record = session.get(ExceptionRecord, exception_id)
        assert record.status == "resolved"
        assert record.resolution == "approve"
        assert record.resolution_reason == "verified against the check register"
        audit = session.query(AuditLog).filter_by(action="exception.resolve").one()
        assert audit.actor == "maria"
        assert audit.detail["exception_id"] == exception_id
        assert audit.detail["resolution"] == "approve"


@pytest.mark.parametrize("resolution", ["approve", "reject", "edit"])
def test_all_valid_resolutions_accepted(tmp_path, resolution):
    engine, exception_id = make_engine_with_exception(tmp_path)
    result = resolve_exception(engine, exception_id, resolution, "reviewed", "sam")
    assert result["resolution"] == resolution


def test_invalid_resolution_raises(tmp_path):
    engine, exception_id = make_engine_with_exception(tmp_path)
    with pytest.raises(ValueError, match="invalid resolution"):
        resolve_exception(engine, exception_id, "defer", "later", "sam")


def test_empty_reason_raises(tmp_path):
    engine, exception_id = make_engine_with_exception(tmp_path)
    with pytest.raises(ValueError, match="reason is mandatory"):
        resolve_exception(engine, exception_id, "approve", "   ", "sam")


def test_unknown_exception_raises(tmp_path):
    engine, _ = make_engine_with_exception(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        resolve_exception(engine, 9999, "approve", "reviewed", "sam")


def test_double_resolution_raises(tmp_path):
    engine, exception_id = make_engine_with_exception(tmp_path)
    resolve_exception(engine, exception_id, "approve", "reviewed", "sam")
    with pytest.raises(ValueError, match="already resolved"):
        resolve_exception(engine, exception_id, "reject", "changed my mind", "sam")
