"""Human resolution of exceptions: the open to resolved transition, audited."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from sentinel.db import AuditLog, ExceptionRecord, get_session

VALID_RESOLUTIONS = ("approve", "reject", "edit")

_REASON_MAX = 500  # matches the exceptions.resolution_reason column width


def resolve_exception(
    engine: Engine,
    exception_id: int,
    resolution: str,
    reason: str,
    actor: str,
) -> dict:
    """Resolve an open exception with a mandatory reason and audit the action.

    Returns the updated record fields as a dict. Raises ValueError for an
    unknown id, an already-resolved exception, an invalid resolution, or an
    empty reason.
    """
    if resolution not in VALID_RESOLUTIONS:
        raise ValueError(
            f"invalid resolution {resolution!r}; expected one of {', '.join(VALID_RESOLUTIONS)}"
        )
    if not reason or not reason.strip():
        raise ValueError("a resolution reason is mandatory")

    with get_session(engine) as session:
        record = session.get(ExceptionRecord, exception_id)
        if record is None:
            raise ValueError(f"exception {exception_id} not found")
        if record.status != "open":
            raise ValueError(f"exception {exception_id} is already {record.status}")

        record.status = "resolved"
        record.resolution = resolution
        record.resolution_reason = reason.strip()[:_REASON_MAX]
        session.add(
            AuditLog(
                actor=actor,
                action="exception.resolve",
                detail={
                    "exception_id": record.id,
                    "period": record.period,
                    "category": record.category,
                    "resolution": resolution,
                    "reason": record.resolution_reason,
                },
            )
        )
        return {
            "id": record.id,
            "status": record.status,
            "resolution": record.resolution,
            "resolution_reason": record.resolution_reason,
        }
