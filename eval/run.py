"""Headless evaluation of the three-month close.

Generates the synthetic company, runs the close for each period, simulates
the human review loop between months (resolving every needs_review JE and
open exception against the planted ground truth), distills and promotes
policy rules through the regression gate, and scores detections against
ground truth.

The LLM is optional. Every model call goes through a counting wrapper around
sentinel.llm.complete; with --llm auto (the default) the model is only
attempted when ANTHROPIC_API_KEY is set, so a bare machine never shells out
to a CLI and every would-be call fails fast with LLMError. Callers already
degrade on LLMError (triage escalates, distillation falls back to the
deterministic templates in eval.review), so the full run completes with no
key and no CLI.

Usage:
    uv run python -m eval.run --db data/eval.db --seed 42 --out eval/results.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from eval import review, scoring
from sentinel import llm
from sentinel.agent.close import run_close
from sentinel.datagen import generate
from sentinel.db import ExceptionRecord, ProposedJERow, get_engine, get_session


class LLMCounter:
    """Counts attempted and succeeded model calls through sentinel.llm.complete."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.attempted = 0
        self.succeeded = 0
        self._original = None

    def install(self) -> None:
        self._original = llm.complete

        def counted(prompt: str, system: str | None = None, model: str | None = None) -> str:
            self.attempted += 1
            if not self.enabled:
                raise llm.LLMError("LLM disabled for this eval run")
            result = self._original(prompt, system=system, model=model)
            self.succeeded += 1
            return result

        llm.complete = counted

    def restore(self) -> None:
        if self._original is not None:
            llm.complete = self._original
            self._original = None

    def snapshot(self) -> tuple[int, int]:
        return self.attempted, self.succeeded

    def delta(self, snapshot: tuple[int, int]) -> dict:
        return {
            "attempted": self.attempted - snapshot[0],
            "succeeded": self.succeeded - snapshot[1],
        }


def _llm_enabled(mode: str) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def collect_detections(engine: Engine, periods: list[str]) -> list[dict]:
    """All scored detections: every exception plus JEs claiming a category."""
    detections: list[dict] = []
    with get_session(engine) as session:
        exception_rows = (
            session.query(ExceptionRecord)
            .filter(ExceptionRecord.period.in_(periods))
            .order_by(ExceptionRecord.id)
            .all()
        )
        for record in exception_rows:
            detections.append(
                {
                    "period": record.period,
                    "category": record.category,
                    "kind": "exception",
                    "targets": review.detection_targets(
                        record.source_table, record.row_id, record.description
                    ),
                }
            )
        je_rows = (
            session.query(ProposedJERow)
            .filter(
                ProposedJERow.period.in_(periods),
                ProposedJERow.rule.in_(sorted(scoring.SCORED_JE_RULES)),
            )
            .order_by(ProposedJERow.id)
            .all()
        )
        for row in je_rows:
            detections.append(
                {
                    "period": row.period,
                    "category": row.rule,
                    "kind": "je",
                    "targets": {
                        (item["source_table"], item["row_id"]) for item in row.evidence_json
                    },
                }
            )
    return detections


def run_eval(
    db_path: str,
    seed: int,
    out_path: str | None,
    ground_truth_path: str = "data/ground_truth.json",
    months: int = 3,
    llm_mode: str = "auto",
    charts_dir: str | None = None,
) -> dict:
    """Run the full headless close eval and return the results dict."""
    summary = generate(db_path, seed=seed, ground_truth_path=ground_truth_path)
    ground_truth = summary["ground_truth"]
    periods = sorted(ground_truth)[:months]

    engine = get_engine(db_path)
    counter = LLMCounter(enabled=_llm_enabled(llm_mode))
    month_results: list[dict] = []
    try:
        gt_index = review.load_ground_truth_index(engine, ground_truth)
        counter.install()
        closed: list[str] = []
        for i, period in enumerate(periods):
            prior = periods[i - 1] if i else None
            snap = counter.snapshot()
            started = time.perf_counter()
            close_result = run_close(engine, period, prior_period=prior)
            reviewed = review.review_period(engine, period, gt_index)
            closed.append(period)
            learned = review.learn_from_period(
                engine, reviewed["approved_exception_ids"], closed
            )
            wall = round(time.perf_counter() - started, 3)
            decisions = close_result.je_count + close_result.exception_count
            escalated = close_result.needs_review_count + close_result.exception_count
            month_results.append(
                {
                    "period": period,
                    "je_count": close_result.je_count,
                    "auto_approved_count": close_result.auto_approved_count,
                    "needs_review_count": close_result.needs_review_count,
                    "exception_count": close_result.exception_count,
                    "escalation_rate": round(escalated / decisions, 4) if decisions else 0.0,
                    "auto_match_rate": close_result.stats.get("recon", {}).get(
                        "auto_match_rate"
                    ),
                    "wall_time_seconds": wall,
                    "llm_calls": counter.delta(snap),
                    "review": reviewed["counts"],
                    "rules": learned,
                }
            )
        counter.restore()

        detections = collect_detections(engine, periods)
        scores = scoring.score(gt_index, detections, periods)
    finally:
        counter.restore()
        engine.dispose()

    for month in month_results:
        month["scores"] = scores["per_month"].get(month["period"], {})

    results = {
        "meta": {
            "seed": seed,
            "db_path": db_path,
            "ground_truth_path": ground_truth_path,
            "periods": periods,
            "llm_mode": llm_mode,
            "llm_enabled": counter.enabled,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "months": month_results,
        "by_category": scores["by_category"],
        "totals": scores["totals"],
        "escalation_by_month": {
            month["period"]: month["escalation_rate"] for month in month_results
        },
        "llm_calls_total": {"attempted": counter.attempted, "succeeded": counter.succeeded},
    }

    if out_path:
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
            handle.write("\n")

    if charts_dir:
        from eval import charts

        os.makedirs(charts_dir, exist_ok=True)
        charts.render_escalation(os.path.join(charts_dir, "escalation.png"), month_results)
        charts.render_f1_by_category(
            os.path.join(charts_dir, "f1_by_category.png"), results["by_category"]
        )

    return results


def print_summary(results: dict) -> None:
    """Compact stdout summary: per-month table, per-category table, headline."""
    meta = results["meta"]
    print(f"Ledger Sentinel eval  seed={meta['seed']}  llm={meta['llm_mode']}"
          f" (enabled={meta['llm_enabled']})")
    print()
    header = (
        f"{'month':<9}{'tp':>4}{'fp':>4}{'fn':>4}{'prec':>7}{'rec':>7}{'f1':>7}"
        f"{'escal':>8}{'match':>8}{'llm a/s':>9}{'wall_s':>8}"
    )
    print(header)
    print("-" * len(header))
    for month in results["months"]:
        totals = month["scores"].get("totals", scoring.prf(0, 0, 0))
        llm_calls = month["llm_calls"]
        match_rate = month["auto_match_rate"]
        print(
            f"{month['period']:<9}"
            f"{totals['tp']:>4}{totals['fp']:>4}{totals['fn']:>4}"
            f"{totals['precision']:>7.2f}{totals['recall']:>7.2f}{totals['f1']:>7.2f}"
            f"{month['escalation_rate']:>8.3f}"
            f"{(match_rate if match_rate is not None else 0.0):>8.3f}"
            f"{llm_calls['attempted']:>5}/{llm_calls['succeeded']:<3}"
            f"{month['wall_time_seconds']:>8.2f}"
        )
    print()
    print(f"{'category':<24}{'tp':>4}{'fp':>4}{'fn':>4}{'prec':>7}{'rec':>7}{'f1':>7}")
    print("-" * 57)
    for category, metrics in sorted(results["by_category"].items()):
        print(
            f"{category:<24}{metrics['tp']:>4}{metrics['fp']:>4}{metrics['fn']:>4}"
            f"{metrics['precision']:>7.2f}{metrics['recall']:>7.2f}{metrics['f1']:>7.2f}"
        )
    print()
    rates = [f"{p} {r:.3f}" for p, r in results["escalation_by_month"].items()]
    print("Escalation rate by month: " + "  ->  ".join(rates))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the headless close evaluation.")
    parser.add_argument("--db", default="data/eval.db", help="SQLite database path")
    parser.add_argument("--seed", type=int, default=42, help="Data generator seed")
    parser.add_argument("--out", default="eval/results.json", help="Results JSON path")
    parser.add_argument(
        "--ground-truth", default="data/ground_truth.json", help="Ground truth JSON path"
    )
    parser.add_argument("--months", type=int, default=3, help="Number of periods to close")
    parser.add_argument(
        "--llm",
        choices=("auto", "on", "off"),
        default="auto",
        help="auto: use the model only when ANTHROPIC_API_KEY is set; on: always; off: never",
    )
    parser.add_argument("--charts-dir", default="eval", help="Directory for the PNG charts")
    parser.add_argument("--no-charts", action="store_true", help="Skip chart rendering")
    args = parser.parse_args(argv)

    results = run_eval(
        db_path=args.db,
        seed=args.seed,
        out_path=args.out,
        ground_truth_path=args.ground_truth,
        months=args.months,
        llm_mode=args.llm,
        charts_dir=None if args.no_charts else args.charts_dir,
    )
    print_summary(results)


if __name__ == "__main__":
    main()
