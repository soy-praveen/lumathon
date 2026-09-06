"""Command line entry point: uv run python -m sentinel.datagen"""

import argparse

from sentinel.datagen.generator import (
    DEFAULT_DB_PATH,
    DEFAULT_GROUND_TRUTH_PATH,
    generate,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.datagen",
        description="Generate the synthetic company database and anomaly ground truth.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite file to (re)create")
    parser.add_argument("--seed", type=int, default=42, help="deterministic RNG seed")
    parser.add_argument(
        "--ground-truth",
        default=DEFAULT_GROUND_TRUTH_PATH,
        help="where to write the planted anomaly labels",
    )
    args = parser.parse_args(argv)

    summary = generate(args.db, seed=args.seed, ground_truth_path=args.ground_truth)
    print(f"wrote {summary['db_path']} (seed {summary['seed']})")
    for table, count in summary["counts"].items():
        print(f"  {table}: {count}")
    planted = sum(len(entries) for entries in summary["ground_truth"].values())
    print(f"planted {planted} anomalies across {len(summary['ground_truth'])} periods")
    print(f"ground truth: {summary['ground_truth_path']}")


if __name__ == "__main__":
    main()
