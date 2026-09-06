"""Database engine, session helpers, and ORM tables for the synthetic ERP.

Other modules add no tables here; they build on these. The schema is the shared
contract for the matching, accrual, flux, and anomaly engines.

Amounts are floats of currency units; compare with a cent-level tolerance,
never exact equality.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import JSON, ForeignKey, Numeric, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DEFAULT_DB_PATH = "sentinel.db"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(30))  # asset, liability, equity, revenue, expense


class GLEntry(Base):
    __tablename__ = "gl_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    entry_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    account_code: Mapped[str] = mapped_column(String(20), index=True)
    description: Mapped[str] = mapped_column(String(255))
    debit: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), default=0)
    credit: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), default=0)
    source: Mapped[str | None] = mapped_column(String(50), default=None)


class BankLine(Base):
    __tablename__ = "bank_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    line_date: Mapped[str] = mapped_column(String(10))
    descriptor: Mapped[str] = mapped_column(String(255))  # raw bank statement text
    # amount is positive for deposits, negative for withdrawals
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    reference: Mapped[str | None] = mapped_column(String(80), default=None)


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    payment_terms: Mapped[str | None] = mapped_column(String(30), default=None)
    default_expense_account: Mapped[str | None] = mapped_column(String(20), default=None)


class APInvoice(Base):
    __tablename__ = "ap_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    invoice_number: Mapped[str] = mapped_column(String(60))
    invoice_date: Mapped[str] = mapped_column(String(10))
    due_date: Mapped[str | None] = mapped_column(String(10), default=None)
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, paid, void


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    po_number: Mapped[str] = mapped_column(String(60))
    order_date: Mapped[str] = mapped_column(String(10))
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, received, closed


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"))
    receipt_date: Mapped[str] = mapped_column(String(10))
    amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))


class RecurringVendor(Base):
    __tablename__ = "recurring_vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    expected_amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    expected_day: Mapped[int]  # day of month the bill usually lands
    account_code: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(default=True)


class DodoPayout(Base):
    __tablename__ = "dodo_payouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    payout_date: Mapped[str] = mapped_column(String(10))
    gross_amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    fee_amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    net_amount: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    reference: Mapped[str | None] = mapped_column(String(80), default=None)


class ProposedJERow(Base):
    __tablename__ = "proposed_jes"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    rule: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[float]
    status: Mapped[str] = mapped_column(String(20))  # see schemas.ProposedJE
    evidence_json: Mapped[list] = mapped_column(JSON)  # serialized schemas.Evidence list
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class JELineRow(Base):
    __tablename__ = "je_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    je_id: Mapped[int] = mapped_column(ForeignKey("proposed_jes.id"))
    account_code: Mapped[str] = mapped_column(String(20))
    debit: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), default=0)
    credit: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False), default=0)


class ExceptionRecord(Base):
    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    category: Mapped[str] = mapped_column(String(50))  # recon, accrual, flux, anomaly
    description: Mapped[str] = mapped_column(String(500))
    source_table: Mapped[str | None] = mapped_column(String(50), default=None)
    row_id: Mapped[int | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, resolved
    # resolution is one of: approve, reject, edit
    resolution: Mapped[str | None] = mapped_column(String(20), default=None)
    resolution_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class PolicyRuleRow(Base):
    __tablename__ = "policy_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(120))
    condition: Mapped[str] = mapped_column(String(500))
    action: Mapped[str] = mapped_column(String(500))
    limits_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    active: Mapped[bool] = mapped_column(default=True)
    expires: Mapped[str | None] = mapped_column(String(7), default=None)  # YYYY-MM
    source_exception_id: Mapped[int | None] = mapped_column(
        ForeignKey("exceptions.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=_utcnow)
    actor: Mapped[str] = mapped_column(String(60))  # agent, human username
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[dict | None] = mapped_column(JSON, default=None)


def get_engine(path: str | None = None) -> Engine:
    """Create a SQLite engine. Falls back to the SENTINEL_DB env var, then a local file."""
    if path is None:
        path = os.environ.get("SENTINEL_DB", DEFAULT_DB_PATH)
    return create_engine(f"sqlite:///{path}")


def init_db(engine: Engine) -> None:
    """Create all tables if they do not exist."""
    Base.metadata.create_all(engine)


@contextmanager
def get_session(engine: Engine) -> Iterator[Session]:
    """Yield a session, committing on success and rolling back on error."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
