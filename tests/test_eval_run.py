"""Smoke test: the harness end to end on one generated month, LLM off."""

import json

from eval.run import run_eval


def test_one_month_smoke(tmp_path):
    db_path = str(tmp_path / "eval.db")
    gt_path = str(tmp_path / "ground_truth.json")
    out_path = str(tmp_path / "results.json")

    results = run_eval(
        db_path=db_path,
        seed=42,
        out_path=out_path,
        ground_truth_path=gt_path,
        months=1,
        llm_mode="off",
        charts_dir=None,
    )

    assert results["meta"]["periods"] == ["2026-01"]
    assert results["meta"]["seed"] == 42
    assert not results["meta"]["llm_enabled"]

    assert len(results["months"]) == 1
    month = results["months"][0]
    assert month["period"] == "2026-01"
    for key in (
        "je_count",
        "needs_review_count",
        "exception_count",
        "escalation_rate",
        "auto_match_rate",
        "wall_time_seconds",
        "llm_calls",
        "review",
        "rules",
        "scores",
    ):
        assert key in month
    assert 0.0 <= month["escalation_rate"] <= 1.0
    assert 0.0 <= month["auto_match_rate"] <= 1.0
    assert month["je_count"] > 0
    assert month["exception_count"] > 0

    # with the LLM off, every attempted call must have failed over
    assert results["llm_calls_total"]["succeeded"] == 0
    assert results["llm_calls_total"]["attempted"] >= 1

    # the review loop resolved everything that escalated
    reviewed = month["review"]
    assert reviewed["jes_approved"] + reviewed["jes_rejected"] == month["needs_review_count"]
    assert (
        reviewed["exceptions_approved"] + reviewed["exceptions_rejected"]
        == month["exception_count"]
    )

    # every planted January anomaly is either detected or a counted miss
    with open(gt_path, encoding="utf-8") as handle:
        ground_truth = json.load(handle)
    totals = month["scores"]["totals"]
    assert totals["tp"] + totals["fn"] == len(ground_truth["2026-01"])

    with open(out_path, encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["escalation_by_month"] == {"2026-01": month["escalation_rate"]}


def test_charts_render(tmp_path):
    from eval import charts

    months = [
        {"period": "2026-01", "escalation_rate": 0.5},
        {"period": "2026-02", "escalation_rate": 0.4},
        {"period": "2026-03", "escalation_rate": 0.3},
    ]
    by_category = {
        "bank_fee": {"f1": 1.0},
        "duplicate_invoice": {"f1": 0.8},
        "timing_difference": {"f1": 0.6667},
    }
    escalation_path = tmp_path / "escalation.png"
    f1_path = tmp_path / "f1_by_category.png"
    charts.render_escalation(str(escalation_path), months)
    charts.render_f1_by_category(str(f1_path), by_category)
    assert escalation_path.stat().st_size > 0
    assert f1_path.stat().st_size > 0
