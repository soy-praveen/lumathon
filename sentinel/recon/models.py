"""Result model for the reconciliation engine."""

from __future__ import annotations

from pydantic import BaseModel, Field

from sentinel.schemas import ProposedJE


class ReconResult(BaseModel):
    """Outcome of one reconciliation run for a single period.

    matches: one dict per automatic match with keys ``bank_line_ids``,
        ``gl_entry_ids``, ``match_type`` (one of ``exact``, ``fuzzy``,
        ``one_to_many``, ``rule``), and ``confidence``.
    proposed_jes: journal entries proposed for bank-only activity. Every entry
        cites evidence rows and balances by construction.
    exceptions: unresolved items as plain dicts with keys ``category``,
        ``description``, ``source_table``, ``row_id``, ``period``.
    stats: ``auto_match_rate`` (bank lines resolved automatically, by a match
        or a proposed JE, divided by total bank lines in the period; 1.0 when
        the period has no bank lines), ``matched_count`` (number of match
        records), ``unmatched_bank``, and ``unmatched_gl``.
    """

    matches: list[dict] = Field(default_factory=list)
    proposed_jes: list[ProposedJE] = Field(default_factory=list)
    exceptions: list[dict] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
