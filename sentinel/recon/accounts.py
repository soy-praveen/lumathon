"""Chart-of-accounts lookups for reconciliation JEs.

Accounts are resolved by name keywords against the accounts table, preferring
a matching account type, and fall back to conventional codes when the chart
does not carry a recognizable account. Lookups scan accounts in id order, so
resolution is deterministic.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from sentinel.db import Account

CASH_FALLBACK = "1000"
CLEARING_FALLBACK = "1090"
REVENUE_FALLBACK = "4000"
INTEREST_INCOME_FALLBACK = "4500"
BANK_FEES_FALLBACK = "6100"
PROCESSING_FEES_FALLBACK = "6110"


def cash_account_codes(session: Session) -> list[str]:
    """All asset accounts with 'cash' in the name; falls back to code 1000."""
    codes = [
        account.code
        for account in session.query(Account).order_by(Account.id).all()
        if account.type == "asset" and "cash" in account.name.lower()
    ]
    return codes or [CASH_FALLBACK]


def resolve_accounts(session: Session) -> dict[str, str]:
    """Map the JE roles the recon engine books to onto account codes."""
    rows = session.query(Account).order_by(Account.id).all()

    def find(keywords: tuple[str, ...], types: tuple[str, ...], fallback: str) -> str:
        for row in rows:
            name = row.name.lower()
            if row.type in types and any(keyword in name for keyword in keywords):
                return row.code
        for row in rows:
            if any(keyword in row.name.lower() for keyword in keywords):
                return row.code
        return fallback

    return {
        "cash": cash_account_codes(session)[0],
        "clearing": find(("clearing", "transit"), ("asset",), CLEARING_FALLBACK),
        "revenue": find(("revenue", "sales"), ("revenue",), REVENUE_FALLBACK),
        "interest_income": find(("interest",), ("revenue",), INTEREST_INCOME_FALLBACK),
        "bank_fees": find(("bank fee", "bank charge"), ("expense",), BANK_FEES_FALLBACK),
        "processing_fees": find(
            ("processing", "merchant"), ("expense",), PROCESSING_FEES_FALLBACK
        ),
    }
