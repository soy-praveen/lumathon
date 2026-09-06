"""Deterministic matching passes: exact, fuzzy, one-to-many.

Bank line amounts are positive for deposits and negative for withdrawals; a GL
cash entry's signed amount is debit minus credit, so the two compare directly.
Passes run in order and matched rows drop out of later passes. All iteration
orders are fixed (date, then row id), so results are deterministic.
"""

from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations

from sentinel.db import BankLine, GLEntry

AMOUNT_TOLERANCE = 0.01  # one cent
EXACT_SIMILARITY = 0.8
# The fuzzy pass gates on amount within tolerance and date within the window;
# descriptor_score only ranks candidates and rejects genuinely unrelated text.
# Measured on the seed-42 synthetic months: every true bank/GL pair scores at
# least 0.80 on descriptor_score while the highest-scoring unrelated pair
# reaches 0.667, so 0.72 splits the two populations with margin on both sides.
FUZZY_SIMILARITY = 0.72
FUZZY_DATE_WINDOW_DAYS = 3
COMBO_MAX_SIZE = 4
COMBO_POOL_CAP = 12

EXACT_CONFIDENCE = 1.0
ONE_TO_MANY_CONFIDENCE = 0.8

# Banking-rail and legal-suffix tokens carry no counterparty identity, so they
# are excluded from token-level scoring ("MONTHLY SERVICE FEE" must not pair
# with "Amazon Web Services" on SERVICE vs SERVICES).
STOP_TOKENS = frozenset(
    {
        "PAYMENT", "PAYMENTS", "PMT", "PAYROLL", "PAYOUT", "TRANSFER",
        "DEPOSIT", "WITHDRAWAL", "WIRE", "CHECK", "DEBIT", "CREDIT",
        "FEE", "FEES", "CHARGE", "SERVICE", "SERVICES", "MONTHLY",
        "INV", "INVOICE", "BILL", "BATCH", "INC", "LLC", "LLP", "CORP",
        "COMPANY", "THE", "COM", "AND", "FOR",
    }
)


def normalize(text: str) -> str:
    """Uppercase and collapse everything that is not a letter or digit."""
    return re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).strip()


def similarity(a: str, b: str) -> float:
    """Descriptor similarity in [0, 1]; containment counts as near-identical."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    if na in nb or nb in na:
        ratio = max(ratio, 0.9)
    return ratio


def _distinctive_tokens(text: str) -> list[str]:
    """Tokens that can identify a counterparty: 3+ letters, no digits, not rail noise."""
    return [
        token
        for token in normalize(text).split()
        if len(token) >= 3
        and token not in STOP_TOKENS
        and not any(ch.isdigit() for ch in token)
    ]


def descriptor_score(bank_text: str, gl_text: str) -> float:
    """Best of whole-string similarity and distinctive-token similarity.

    A bank descriptor and its GL description usually share a vendor token even
    when the whole strings diverge ("WEWORK COMMONS BTRQJ" vs "Payment to
    WeWork INV-WW-2601754"), so the best token pair competes with the
    whole-string ratio. Token pairs where both sides are shorter than four
    characters are too weak to identify a vendor and are skipped.
    """
    best = similarity(bank_text, gl_text)
    for bank_token in _distinctive_tokens(bank_text):
        for gl_token in _distinctive_tokens(gl_text):
            if len(bank_token) < 4 and len(gl_token) < 4:
                continue
            best = max(best, SequenceMatcher(None, bank_token, gl_token).ratio())
    return best


def gl_signed_amount(entry: GLEntry) -> float:
    return (entry.debit or 0.0) - (entry.credit or 0.0)


def amounts_equal(a: float, b: float) -> bool:
    return abs(a - b) <= AMOUNT_TOLERANCE


def _days_apart(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def run_matching_passes(
    bank_lines: list[BankLine], gl_entries: list[GLEntry]
) -> tuple[list[dict], list[BankLine], list[GLEntry]]:
    """Run exact, fuzzy, and one-to-many passes.

    Returns (matches, remaining_bank_lines, remaining_gl_entries).
    """
    bank = sorted(bank_lines, key=lambda b: (b.line_date, b.id))
    gl = sorted(gl_entries, key=lambda g: (g.entry_date, g.id))
    matched_gl: set[int] = set()

    matches: list[dict] = []
    for match_type in ("exact", "fuzzy"):
        pass_matches, bank = _pair_pass(bank, gl, matched_gl, match_type=match_type)
        matches.extend(pass_matches)
    pass_matches, bank = _one_to_many_pass(bank, gl, matched_gl)
    matches.extend(pass_matches)

    remaining_gl = [entry for entry in gl if entry.id not in matched_gl]
    return matches, bank, remaining_gl


def _pair_pass(
    bank: list[BankLine], gl: list[GLEntry], matched_gl: set[int], *, match_type: str
) -> tuple[list[dict], list[BankLine]]:
    matches: list[dict] = []
    remaining: list[BankLine] = []
    for line in bank:
        best: tuple[tuple, GLEntry, float] | None = None
        for entry in gl:
            if entry.id in matched_gl:
                continue
            if not amounts_equal(line.amount, gl_signed_amount(entry)):
                continue
            days = _days_apart(line.line_date, entry.entry_date)
            if match_type == "exact":
                sim = similarity(line.descriptor, entry.description)
                if days != 0 or sim < EXACT_SIMILARITY:
                    continue
            else:
                sim = descriptor_score(line.descriptor, entry.description)
                if days > FUZZY_DATE_WINDOW_DAYS or sim < FUZZY_SIMILARITY:
                    continue
            key = (sim, -days, -entry.id)
            if best is None or key > best[0]:
                best = (key, entry, sim)
        if best is None:
            remaining.append(line)
            continue
        _, entry, sim = best
        matched_gl.add(entry.id)
        # Fuzzy confidence scales with the descriptor score: a threshold-level
        # match lands below the 0.85 auto-approve band, a perfect score at 0.9.
        confidence = EXACT_CONFIDENCE if match_type == "exact" else round(0.5 + 0.4 * sim, 4)
        matches.append(
            {
                "bank_line_ids": [line.id],
                "gl_entry_ids": [entry.id],
                "match_type": match_type,
                "confidence": confidence,
            }
        )
    return matches, remaining


def _one_to_many_pass(
    bank: list[BankLine], gl: list[GLEntry], matched_gl: set[int]
) -> tuple[list[dict], list[BankLine]]:
    matches: list[dict] = []
    remaining: list[BankLine] = []
    for line in bank:
        available = [entry for entry in gl if entry.id not in matched_gl]
        combo = _find_combo(line, available)
        if combo is None:
            remaining.append(line)
            continue
        matched_gl.update(entry.id for entry in combo)
        matches.append(
            {
                "bank_line_ids": [line.id],
                "gl_entry_ids": [entry.id for entry in combo],
                "match_type": "one_to_many",
                "confidence": ONE_TO_MANY_CONFIDENCE,
            }
        )
    return matches, remaining


def _find_combo(line: BankLine, entries: list[GLEntry]) -> list[GLEntry] | None:
    """Find GL entries from the same day or same vendor summing to the bank amount."""
    pools: dict[tuple[str, str], list[GLEntry]] = {}
    for entry in entries:
        pools.setdefault(("date", entry.entry_date), []).append(entry)
        pools.setdefault(("desc", normalize(entry.description)), []).append(entry)
    for key in sorted(pools):
        pool = pools[key][:COMBO_POOL_CAP]
        if len(pool) < 2:
            continue
        for size in range(2, min(COMBO_MAX_SIZE, len(pool)) + 1):
            for combo in combinations(pool, size):
                total = sum(gl_signed_amount(entry) for entry in combo)
                if amounts_equal(total, line.amount):
                    return list(combo)
    return None
