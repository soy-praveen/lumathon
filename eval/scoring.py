"""Score engine detections against the planted-anomaly ground truth.

A detection is either an exception (its category plus the row it points at,
plus any partner rows named in its description) or a proposed JE (its rule
field is the category claim and its evidence rows are the targets). A ground
truth row counts as detected when some detection claims its category and
cites one of its acceptable rows.

Matching runs across all closed periods because a planted anomaly can
legitimately surface in the following month: a timing difference planted in
January is first an unmatched GL cash entry at the January close and then a
timing JE when the bank line clears in February, both citing the same GL row.
True and false negatives are counted in the ground truth row's own period;
false positives in the detection's period under the detection's own category.
"""

from __future__ import annotations

# JE rule values that assert one of the planted anomaly categories. Other
# rules (dodo_payout, policy:*) are routine bookings, not anomaly claims, so
# they are not scored.
SCORED_JE_RULES = frozenset(
    {"bank_fee", "bank_interest", "timing_difference", "rni_po", "missing_recurring"}
)

# A recon_unmatched exception is the engine's generic "something here does not
# tie" claim; it satisfies the bank-side ground truth categories when it
# points at the planted row.
EXCEPTION_CATEGORY_ALIASES = {
    "recon_unmatched": frozenset({"bank_fee", "bank_interest", "timing_difference"}),
}


def claimed_categories(detection: dict) -> frozenset[str]:
    """Ground truth categories this detection can satisfy."""
    category = detection["category"]
    claimed = {category}
    if detection["kind"] == "exception":
        claimed |= EXCEPTION_CATEGORY_ALIASES.get(category, frozenset())
    return frozenset(claimed)


def prf(tp: int, fp: int, fn: int) -> dict:
    """Precision/recall/F1 with zero denominators reported as 0.0."""
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _matches(detection: dict, gt_row: dict) -> bool:
    if gt_row["category"] not in claimed_categories(detection):
        return False
    return bool(set(detection["targets"]) & set(gt_row["targets"]))


def score(gt_rows: list[dict], detections: list[dict], periods: list[str]) -> dict:
    """Score detections against ground truth for the closed `periods`.

    gt_rows: {period, category, targets: [(table, id), ...], description}.
    All ground truth rows take part in matching (so a detection citing a
    prior period's planted row is not a false positive) but only rows whose
    period is in `periods` are counted.

    detections: {period, category, kind: "exception"|"je", targets: [...]}.
    """
    matched_gt: set[int] = set()
    matched_det: set[int] = set()
    for gi, gt_row in enumerate(gt_rows):
        for di, detection in enumerate(detections):
            if _matches(detection, gt_row):
                matched_gt.add(gi)
                matched_det.add(di)

    tp: dict[tuple[str, str], int] = {}
    fn: dict[tuple[str, str], int] = {}
    fp: dict[tuple[str, str], int] = {}
    for gi, gt_row in enumerate(gt_rows):
        if gt_row["period"] not in periods:
            continue
        bucket = tp if gi in matched_gt else fn
        key = (gt_row["period"], gt_row["category"])
        bucket[key] = bucket.get(key, 0) + 1
    for di, detection in enumerate(detections):
        if di in matched_det:
            continue
        key = (detection["period"], detection["category"])
        fp[key] = fp.get(key, 0) + 1

    keys = set(tp) | set(fn) | set(fp)
    per_month: dict[str, dict] = {period: {"categories": {}} for period in periods}
    for period, category in sorted(keys):
        per_month.setdefault(period, {"categories": {}})["categories"][category] = prf(
            tp.get((period, category), 0),
            fp.get((period, category), 0),
            fn.get((period, category), 0),
        )
    for period, month in per_month.items():
        month["totals"] = prf(
            sum(v for (p, _), v in tp.items() if p == period),
            sum(v for (p, _), v in fp.items() if p == period),
            sum(v for (p, _), v in fn.items() if p == period),
        )

    categories = sorted({category for _, category in keys})
    by_category = {
        category: prf(
            sum(v for (_, c), v in tp.items() if c == category),
            sum(v for (_, c), v in fp.items() if c == category),
            sum(v for (_, c), v in fn.items() if c == category),
        )
        for category in categories
    }
    totals = prf(sum(tp.values()), sum(fp.values()), sum(fn.values()))
    return {"per_month": per_month, "by_category": by_category, "totals": totals}
