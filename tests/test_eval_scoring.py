"""Scoring math on small hand-built fixtures with known TP/FP/FN counts."""

from eval import scoring


def gt_row(period, category, table, row_id):
    return {
        "period": period,
        "category": category,
        "targets": {(table, row_id)},
        "description": f"planted {category}",
    }


def detection(period, category, kind, targets):
    return {"period": period, "category": category, "kind": kind, "targets": targets}


def test_prf_math():
    metrics = scoring.prf(2, 1, 1)
    assert metrics["tp"] == 2
    assert metrics["fp"] == 1
    assert metrics["fn"] == 1
    assert metrics["precision"] == round(2 / 3, 4)
    assert metrics["recall"] == round(2 / 3, 4)
    assert metrics["f1"] == round(2 / 3, 4)


def test_prf_zero_denominators():
    metrics = scoring.prf(0, 0, 0)
    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0
    assert metrics["f1"] == 0.0


def test_score_known_counts():
    gt_rows = [
        gt_row("2026-01", "bank_fee", "bank_lines", 10),
        gt_row("2026-01", "duplicate_invoice", "ap_invoices", 5),
        gt_row("2026-01", "timing_difference", "gl_entries", 7),
    ]
    detections = [
        # true positive: JE claiming bank_fee and citing the planted row
        detection("2026-01", "bank_fee", "je", {("bank_lines", 10)}),
        # false positive: exception pointing at a row nobody planted
        detection("2026-01", "duplicate_invoice", "exception", {("ap_invoices", 99)}),
        # true positive: recon_unmatched satisfies timing_difference via alias
        detection("2026-01", "recon_unmatched", "exception", {("gl_entries", 7)}),
    ]
    result = scoring.score(gt_rows, detections, ["2026-01"])

    totals = result["per_month"]["2026-01"]["totals"]
    assert (totals["tp"], totals["fp"], totals["fn"]) == (2, 1, 1)
    assert totals["precision"] == round(2 / 3, 4)
    assert totals["recall"] == round(2 / 3, 4)

    by_category = result["by_category"]
    assert by_category["bank_fee"] == scoring.prf(1, 0, 0)
    assert by_category["duplicate_invoice"] == scoring.prf(0, 1, 1)
    assert by_category["timing_difference"] == scoring.prf(1, 0, 0)
    assert result["totals"] == scoring.prf(2, 1, 1)


def test_duplicate_detections_count_once():
    gt_rows = [gt_row("2026-01", "bank_fee", "bank_lines", 10)]
    detections = [
        detection("2026-01", "bank_fee", "je", {("bank_lines", 10)}),
        detection("2026-01", "recon_unmatched", "exception", {("bank_lines", 10)}),
    ]
    result = scoring.score(gt_rows, detections, ["2026-01"])
    assert result["totals"] == scoring.prf(1, 0, 0)


def test_cross_period_detection_credits_planted_month():
    # A timing difference planted in January is detected by a February JE
    # citing the same GL row; January gets the TP and February gets no FP.
    gt_rows = [gt_row("2026-01", "timing_difference", "gl_entries", 7)]
    detections = [
        detection("2026-02", "timing_difference", "je", {("gl_entries", 7)}),
    ]
    result = scoring.score(gt_rows, detections, ["2026-01", "2026-02"])
    jan = result["per_month"]["2026-01"]["totals"]
    feb = result["per_month"]["2026-02"]["totals"]
    assert (jan["tp"], jan["fn"]) == (1, 0)
    assert feb["fp"] == 0


def test_category_must_match():
    gt_rows = [gt_row("2026-01", "duplicate_invoice", "ap_invoices", 5)]
    detections = [
        detection("2026-01", "round_number_split", "exception", {("ap_invoices", 5)}),
    ]
    result = scoring.score(gt_rows, detections, ["2026-01"])
    assert result["by_category"]["duplicate_invoice"]["fn"] == 1
    assert result["by_category"]["round_number_split"]["fp"] == 1
