"""Reconciliation orchestrator.

Pass order: exact, fuzzy, one-to-many matching against GL cash entries; policy
rule application; Dodo payout reconciliation; heuristic JE proposals for bank
fees, interest, and timing differences. Whatever is left becomes a
``recon_unmatched`` exception. The engine is deterministic and writes nothing
to the database.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from sentinel.db import BankLine, GLEntry
from sentinel.recon.accounts import cash_account_codes, resolve_accounts
from sentinel.recon.dodo import reconcile_payouts
from sentinel.recon.matching import run_matching_passes
from sentinel.recon.models import ReconResult
from sentinel.recon.proposals import propose_bank_only_jes
from sentinel.recon.rules import apply_rules
from sentinel.schemas import PolicyRule

PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def run_recon(
    session: Session, period: str, rules: list[PolicyRule] | None = None
) -> ReconResult:
    """Reconcile one period's bank lines against GL cash entries and Dodo payouts."""
    if not PERIOD_PATTERN.match(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")

    bank_lines = (
        session.query(BankLine)
        .filter(BankLine.period == period)
        .order_by(BankLine.line_date, BankLine.id)
        .all()
    )
    cash_codes = cash_account_codes(session)
    gl_cash = (
        session.query(GLEntry)
        .filter(GLEntry.period == period, GLEntry.account_code.in_(cash_codes))
        .order_by(GLEntry.entry_date, GLEntry.id)
        .all()
    )
    accounts = resolve_accounts(session)

    matches, remaining_bank, remaining_gl = run_matching_passes(bank_lines, gl_cash)
    proposed_jes = []
    exceptions = []

    if rules:
        rule_matches, rule_jes, remaining_bank = apply_rules(
            remaining_bank, rules, period, accounts
        )
        matches.extend(rule_matches)
        proposed_jes.extend(rule_jes)

    remaining_ids = {line.id for line in remaining_bank}
    dodo_jes, dodo_exceptions, remaining_bank = reconcile_payouts(
        session,
        period,
        remaining_bank,
        accounts,
        ledgered_deposits=[line for line in bank_lines if line.id not in remaining_ids],
    )
    proposed_jes.extend(dodo_jes)
    exceptions.extend(dodo_exceptions)

    heuristic_jes, remaining_bank = propose_bank_only_jes(
        session, period, remaining_bank, accounts, cash_codes
    )
    proposed_jes.extend(heuristic_jes)

    for line in remaining_bank:
        exceptions.append(
            {
                "category": "recon_unmatched",
                "description": (
                    f"Bank line '{line.descriptor}' {line.amount:.2f} on {line.line_date} "
                    "has no GL counterpart"
                ),
                "source_table": "bank_lines",
                "row_id": line.id,
                "period": period,
            }
        )
    for entry in remaining_gl:
        exceptions.append(
            {
                "category": "recon_unmatched",
                "description": (
                    f"GL cash entry '{entry.description}' on {entry.entry_date} "
                    "has no bank counterpart"
                ),
                "source_table": "gl_entries",
                "row_id": entry.id,
                "period": period,
            }
        )

    total_bank = len(bank_lines)
    unmatched_bank = len(remaining_bank)
    if total_bank == 0:
        auto_match_rate = 1.0
    else:
        auto_match_rate = round((total_bank - unmatched_bank) / total_bank, 4)
    stats = {
        "auto_match_rate": auto_match_rate,
        "matched_count": len(matches),
        "unmatched_bank": unmatched_bank,
        "unmatched_gl": len(remaining_gl),
    }
    return ReconResult(
        matches=matches, proposed_jes=proposed_jes, exceptions=exceptions, stats=stats
    )
