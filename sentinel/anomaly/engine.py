"""Deterministic anomaly checks over AP invoices, vendors, and GL entries.

Four checks, each emitting plain exception dicts pointing at the offending row,
with partner row ids named in the description:

- duplicate_invoice: same vendor, same or near-same amount, close dates,
  different invoice numbers.
- vendor_name_variance: near-duplicate vendor names via difflib, after
  normalizing case and stripping corporate suffixes (Inc, LLC, Ltd, Co).
  A pair is reported once, in the period the newer vendor first bills.
  A promoted policy rule can suppress a known-benign pair.
- round_number_split: two or more invoices from one vendor just under a round
  approval limit, or a single suspiciously round invoice amount.
- out_of_period: gl_entries whose entry_date falls outside their period column,
  one exception per logical journal entry (double-entry rows deduped).

No LLM calls here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel import tracing
from sentinel.db import APInvoice, GLEntry, Vendor
from sentinel.schemas import PolicyRule

DUP_DATE_WINDOW_DAYS = 10
DUP_AMOUNT_PCT = 0.01  # amounts within 1 percent (or one cent) count as near-same
# Calibrated on real-world variants: after suffix stripping, planted pairs like
# "Datadog" / "Datadog, Inc." compare at 1.0 while the closest distinct vendor
# pair (Salesforce / Staples) sits at 0.59, so 0.8 separates them cleanly.
NAME_SIMILARITY = 0.8
CORPORATE_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "llp", "ltd", "limited", "co", "corp", "corporation", "company"}
)
APPROVAL_LIMITS = (1000.0, 2500.0, 5000.0, 10000.0, 25000.0, 50000.0)
JUST_UNDER_SHARE = 0.9  # an amount in [0.9 * limit, limit) is "just under" the limit
ROUND_AMOUNT_MIN = 5000.0
ROUND_STEP = 1000.0
CENT_TOLERANCE = 0.005


class AnomalyResult(BaseModel):
    exceptions: list[dict]


@tracing.span("TOOL", name="anomaly", tool_name="anomaly_checks")
def run_anomaly(
    session: Session, period: str, rules: list[PolicyRule] | None = None
) -> AnomalyResult:
    """Run all anomaly checks for the period, applying promoted policy rules."""
    exceptions: list[dict] = []
    invoices_by_vendor = _invoices_by_vendor(session, period)
    _check_duplicate_invoices(invoices_by_vendor, period, exceptions)
    _check_vendor_name_variance(session, period, exceptions, rules or [])
    _check_round_number_split(invoices_by_vendor, period, exceptions)
    _check_out_of_period(session, period, exceptions)
    return AnomalyResult(exceptions=exceptions)


def _invoices_by_vendor(session: Session, period: str) -> dict[int, list[APInvoice]]:
    invoices = session.scalars(
        select(APInvoice)
        .where(APInvoice.period == period, APInvoice.status != "void")
        .order_by(APInvoice.id)
    ).all()
    grouped: dict[int, list[APInvoice]] = defaultdict(list)
    for invoice in invoices:
        grouped[invoice.vendor_id].append(invoice)
    return grouped


def _check_duplicate_invoices(
    invoices_by_vendor: dict[int, list[APInvoice]], period: str, exceptions: list[dict]
) -> None:
    for vendor_id in sorted(invoices_by_vendor):
        for first, second in combinations(invoices_by_vendor[vendor_id], 2):
            if first.invoice_number == second.invoice_number:
                continue
            tolerance = max(0.01, DUP_AMOUNT_PCT * max(abs(first.amount), abs(second.amount)))
            if abs(first.amount - second.amount) > tolerance:
                continue
            days_apart = abs(
                (date.fromisoformat(second.invoice_date) - date.fromisoformat(first.invoice_date))
                .days
            )
            if days_apart > DUP_DATE_WINDOW_DAYS:
                continue
            exceptions.append(
                {
                    "category": "duplicate_invoice",
                    "description": (
                        f"Invoice {second.invoice_number} for {second.amount:.2f} dated "
                        f"{second.invoice_date} looks like a duplicate of "
                        f"{first.invoice_number} for {first.amount:.2f} dated "
                        f"{first.invoice_date} from the same vendor "
                        f"(partner ap_invoices row {first.id})."
                    ),
                    "source_table": "ap_invoices",
                    "row_id": second.id,
                    "period": period,
                }
            )


def _check_vendor_name_variance(
    session: Session, period: str, exceptions: list[dict], rules: list[PolicyRule]
) -> None:
    """Flag near-duplicate vendor names, unless a promoted rule covers the pair.

    A rule suppresses a pair when it is active, unexpired for `period`, its
    scope names this check (contains ``vendor_name_variance`` or ``anomaly``,
    case-insensitive), its action starts with ``suppress`` or ``ignore``, and
    its condition covers the pair: after the same normalization the vendor
    names get (lowercase, punctuation stripped, trailing corporate suffixes
    dropped), both vendors' normalized names appear in the normalized
    condition. Suffix variants share one normalized name, so a condition
    naming the shared name (for example ``vendor:Datadog``) covers the pair;
    otherwise the condition must name both vendors. Pairs no rule covers
    still fire.
    """
    vendors = session.scalars(select(Vendor).order_by(Vendor.id)).all()
    first_billed = _first_invoice_periods(session, period)
    active = [rule for rule in rules if _rule_applies_to_variance(rule, period)]
    for first, second in combinations(vendors, 2):
        ratio = SequenceMatcher(None, _normalize_name(first.name), _normalize_name(second.name))
        similarity = ratio.ratio()
        if similarity < NAME_SIMILARITY:
            continue
        # Report the pair only in the period the newer vendor first bills;
        # later closes would just repeat a finding already on record. A vendor
        # with no invoices yet counts as new this period.
        emergence = max(first_billed.get(first.id, period), first_billed.get(second.id, period))
        if emergence != period:
            continue
        if any(_rule_covers_pair(rule, first.name, second.name) for rule in active):
            continue
        exceptions.append(
            {
                "category": "vendor_name_variance",
                "description": (
                    f"Vendor name '{second.name}' is suspiciously close to '{first.name}' "
                    f"(similarity {similarity:.2f}, partner vendors row {first.id}); "
                    "possible duplicate vendor record."
                ),
                "source_table": "vendors",
                "row_id": second.id,
                "period": period,
            }
        )


def _normalize_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in name.lower())
    tokens = cleaned.split()
    while len(tokens) > 1 and tokens[-1] in CORPORATE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


BENIGN_ACTION_PREFIXES = (
    "suppress",
    "ignore",
    "auto-approve",
    "auto approve",
    "approve",
    "accept",
    "treat",
    "resolve",
    "skip",
)


def _rule_applies_to_variance(rule: PolicyRule, period: str) -> bool:
    if not rule.active:
        return False
    if rule.expires is not None and rule.expires < period:
        return False
    scope = rule.scope.lower()
    if not any(key in scope for key in ("vendor_name_variance", "vendor", "anomaly")):
        return False
    action = rule.action.strip().lower()
    return action.startswith(BENIGN_ACTION_PREFIXES)


def _rule_is_suffix_generic(rule: PolicyRule) -> bool:
    """True for rules that say suffix-only name variants are the same vendor.

    Distilled rules describe this in prose ("differs only by a legal entity
    suffix") and usually carry a suffix whitelist in limits. Such a rule is not
    about one named pair; it covers every pair whose names collapse to the
    same normalized form.
    """
    text = f"{rule.condition} {rule.scope}".lower()
    limits = rule.limits or {}
    if any(key in limits for key in ("suffix_whitelist", "allowed_suffix_diff_only")):
        return True
    return "suffix" in text and any(word in text for word in ("entity", "inc", "llc", "ltd"))


def _rule_covers_pair(rule: PolicyRule, first_name: str, second_name: str) -> bool:
    first_norm = _normalize_name(first_name)
    second_norm = _normalize_name(second_name)
    if _rule_is_suffix_generic(rule) and first_norm == second_norm:
        return True
    condition = _normalize_name(rule.condition)
    return all(name in condition for name in (first_norm, second_norm))


def _first_invoice_periods(session: Session, period: str) -> dict[int, str]:
    """Earliest invoice period per vendor, over invoices up to the period under review."""
    rows = session.execute(
        select(APInvoice.vendor_id, func.min(APInvoice.period))
        .where(APInvoice.period <= period, APInvoice.status != "void")
        .group_by(APInvoice.vendor_id)
    ).all()
    return dict(rows)


def _check_round_number_split(
    invoices_by_vendor: dict[int, list[APInvoice]], period: str, exceptions: list[dict]
) -> None:
    for vendor_id in sorted(invoices_by_vendor):
        group = invoices_by_vendor[vendor_id]
        for limit in APPROVAL_LIMITS:
            band = [inv for inv in group if JUST_UNDER_SHARE * limit <= inv.amount < limit]
            if len(band) < 2:
                continue
            total = sum(inv.amount for inv in band)
            partners = ", ".join(str(inv.id) for inv in band[1:])
            exceptions.append(
                {
                    "category": "round_number_split",
                    "description": (
                        f"{len(band)} invoices from one vendor each just under the "
                        f"{limit:.0f} approval limit, summing to {total:.2f}; possible "
                        f"split to dodge approval (partner ap_invoices rows {partners})."
                    ),
                    "source_table": "ap_invoices",
                    "row_id": band[0].id,
                    "period": period,
                }
            )
        for invoice in group:
            if invoice.amount < ROUND_AMOUNT_MIN:
                continue
            remainder = invoice.amount % ROUND_STEP
            if min(remainder, ROUND_STEP - remainder) > CENT_TOLERANCE:
                continue
            exceptions.append(
                {
                    "category": "round_number_split",
                    "description": (
                        f"Invoice {invoice.invoice_number} for exactly {invoice.amount:.2f} "
                        "is a suspiciously round amount for a vendor bill."
                    ),
                    "source_table": "ap_invoices",
                    "row_id": invoice.id,
                    "period": period,
                }
            )


def _check_out_of_period(session: Session, period: str, exceptions: list[dict]) -> None:
    """Flag entries dated outside their period, once per logical journal entry.

    A double-entry posting lands as one gl_entries row per leg sharing date,
    description, and source; flagging each leg would double-report the entry.
    Rows are visited in id order and deduped on that shared key, so the
    exception points at the lowest-id row of the posting.
    """
    entries = session.scalars(
        select(GLEntry).where(GLEntry.period == period).order_by(GLEntry.id)
    ).all()
    seen: set[tuple[str, str, str | None]] = set()
    for entry in entries:
        if entry.entry_date[:7] == entry.period:
            continue
        key = (entry.entry_date, entry.description, entry.source)
        if key in seen:
            continue
        seen.add(key)
        exceptions.append(
            {
                "category": "out_of_period",
                "description": (
                    f"GL entry '{entry.description}' dated {entry.entry_date} is posted "
                    f"to period {entry.period}; the entry date falls outside the period."
                ),
                "source_table": "gl_entries",
                "row_id": entry.id,
                "period": period,
            }
        )
