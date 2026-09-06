"""Deterministic flux analysis.

Per-account net movement (sum of debits minus credits) is computed from
gl_entries for the current and prior periods. Movements beyond BOTH the
percentage and absolute thresholds are flagged. Each flagged movement is
explained by grouping GL detail by description and taking the largest
current-vs-prior differences as drivers (new vendor, missing recurring item,
one-off large entry). Drivers must cover at least COVERAGE_SHARE of the delta;
otherwise the movement is unexplained and raises a flux_unexplained exception.

No LLM calls here. The agent core adds narrative on top of these drivers.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel import tracing
from sentinel.db import APInvoice, GLEntry, RecurringVendor, Vendor

COVERAGE_SHARE = 0.8  # drivers must cover this share of the delta to count as explained
MAX_DRIVERS = 5  # cap the number of drivers cited per movement
ONE_OFF_SHARE = 0.5  # a single entry covering this share of the delta is a one-off


class FluxResult(BaseModel):
    movements: list[dict]
    exceptions: list[dict]


@tracing.span("TOOL", name="flux", tool_name="flux_analysis")
def run_flux(
    session: Session,
    period: str,
    prior_period: str,
    threshold_pct: float = 10.0,
    threshold_abs: float = 5000.0,
) -> FluxResult:
    """Flag and explain per-account movements beyond both thresholds."""
    current_totals = _account_totals(session, period)
    prior_totals = _account_totals(session, prior_period)
    movements: list[dict] = []
    exceptions: list[dict] = []

    for code in sorted(set(current_totals) | set(prior_totals)):
        current = round(current_totals.get(code, 0.0), 2)
        prior = round(prior_totals.get(code, 0.0), 2)
        delta = round(current - prior, 2)
        if abs(delta) <= threshold_abs:
            continue
        if abs(prior) > 0.005:
            pct = round(100.0 * delta / abs(prior), 2)
            if abs(pct) <= threshold_pct:
                continue
        else:
            # No prior balance to compare against; any move past the absolute
            # threshold is flagged and pct is reported as None.
            pct = None

        drivers, covered_share, anchor_row_id = _find_drivers(
            session, code, period, prior_period, delta
        )
        explained = covered_share >= COVERAGE_SHARE
        explanation = "; ".join(d["label"] for d in drivers) or "no underlying transactions found"
        if not explained:
            explanation += (
                f" (drivers cover {covered_share:.0%} of the move,"
                f" below the {COVERAGE_SHARE:.0%} bar)"
            )
        evidence = [
            {"source_table": "gl_entries", "row_id": row_id}
            for d in drivers
            for row_id in d["row_ids"]
        ]
        movements.append(
            {
                "account_code": code,
                "current": current,
                "prior": prior,
                "delta": delta,
                "pct": pct,
                "explained": explained,
                "explanation": explanation,
                "evidence": evidence,
            }
        )
        if not explained:
            exceptions.append(
                {
                    "category": "flux_unexplained",
                    "description": (
                        f"Account {code} moved {delta:+.2f} in {period} vs {prior_period}; "
                        f"identified drivers cover only {covered_share:.0%} of the delta, "
                        f"below the {COVERAGE_SHARE:.0%} bar."
                    ),
                    "source_table": "gl_entries",
                    "row_id": anchor_row_id,
                    "period": period,
                }
            )

    return FluxResult(movements=movements, exceptions=exceptions)


def _account_totals(session: Session, period: str) -> dict[str, float]:
    rows = session.execute(
        select(GLEntry.account_code, func.sum(GLEntry.debit), func.sum(GLEntry.credit))
        .where(GLEntry.period == period)
        .group_by(GLEntry.account_code)
    ).all()
    return {code: float(debit or 0.0) - float(credit or 0.0) for code, debit, credit in rows}


def _find_drivers(
    session: Session, code: str, period: str, prior_period: str, delta: float
) -> tuple[list[dict], float, int | None]:
    current_rows = session.scalars(
        select(GLEntry)
        .where(GLEntry.period == period, GLEntry.account_code == code)
        .order_by(GLEntry.id)
    ).all()
    prior_rows = session.scalars(
        select(GLEntry)
        .where(GLEntry.period == prior_period, GLEntry.account_code == code)
        .order_by(GLEntry.id)
    ).all()

    current_groups = _group_by_description(current_rows)
    prior_groups = _group_by_description(prior_rows)
    diffs: list[dict] = []
    for key in set(current_groups) | set(prior_groups):
        cur = current_groups.get(key)
        pri = prior_groups.get(key)
        diff = round((cur["amount"] if cur else 0.0) - (pri["amount"] if pri else 0.0), 2)
        if abs(diff) < 0.005:
            continue
        source = cur or pri
        diffs.append(
            {
                "key": key,
                "diff": diff,
                "row_ids": source["row_ids"],
                "in_current": cur is not None,
                "in_prior": pri is not None,
                "current_count": cur["count"] if cur else 0,
                "raw": source["raw"],
            }
        )
    diffs.sort(key=lambda d: (-abs(d["diff"]), d["key"]))

    drivers: list[dict] = []
    covered = 0.0
    for diff_item in diffs[:MAX_DRIVERS]:
        covered += diff_item["diff"]
        diff_item["label"] = _label_driver(session, diff_item, delta, prior_period)
        drivers.append(diff_item)
        if delta and covered / delta >= COVERAGE_SHARE:
            break
    covered_share = covered / delta if delta else 1.0

    anchor_rows = current_rows or prior_rows
    anchor_row_id = None
    if anchor_rows:
        anchor_row_id = max(anchor_rows, key=lambda e: abs(e.debit - e.credit)).id
    return drivers, covered_share, anchor_row_id


def _group_by_description(rows: list[GLEntry]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for row in rows:
        raw = (row.description or "").strip()
        key = raw.lower()
        group = groups.setdefault(key, {"amount": 0.0, "row_ids": [], "count": 0, "raw": raw})
        group["amount"] += row.debit - row.credit
        group["row_ids"].append(row.id)
        group["count"] += 1
    return groups


def _label_driver(session: Session, driver: dict, delta: float, prior_period: str) -> str:
    name = driver["raw"]
    diff = driver["diff"]
    if driver["in_current"] and not driver["in_prior"]:
        vendor = _vendor_named_in(session, name)
        if vendor is not None and not _vendor_active_in_period(session, vendor, prior_period):
            return f"new vendor {vendor.name}: {diff:+.2f}"
        if driver["current_count"] == 1 and abs(diff) >= ONE_OFF_SHARE * abs(delta):
            return f"one-off large entry '{name}': {diff:+.2f}"
        return f"new item '{name}': {diff:+.2f}"
    if driver["in_prior"] and not driver["in_current"]:
        if _matches_recurring_vendor(session, name):
            return f"missing recurring item '{name}': {diff:+.2f}"
        return f"item no longer present '{name}': {diff:+.2f}"
    return f"change in '{name}': {diff:+.2f}"


def _vendor_named_in(session: Session, text: str) -> Vendor | None:
    lowered = text.lower()
    for vendor in session.scalars(select(Vendor).order_by(Vendor.id)).all():
        if vendor.name.lower() in lowered:
            return vendor
    return None


def _vendor_active_in_period(session: Session, vendor: Vendor, period: str) -> bool:
    invoice = session.scalars(
        select(APInvoice).where(APInvoice.period == period, APInvoice.vendor_id == vendor.id)
    ).first()
    if invoice is not None:
        return True
    name = vendor.name.lower()
    entries = session.scalars(select(GLEntry).where(GLEntry.period == period)).all()
    return any(name in (entry.description or "").lower() for entry in entries)


def _matches_recurring_vendor(session: Session, text: str) -> bool:
    lowered = text.lower()
    recurring = session.scalars(
        select(RecurringVendor).where(RecurringVendor.active.is_(True))
    ).all()
    for rv in recurring:
        vendor = session.get(Vendor, rv.vendor_id)
        if vendor is not None and vendor.name.lower() in lowered:
            return True
    return False
