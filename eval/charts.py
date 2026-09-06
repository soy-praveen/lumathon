"""Render the eval result charts with matplotlib defaults.

Both charts are single-series, so no legend is needed; identity lives in the
title and axis labels. Kept deliberately plain: default style, labeled axes.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402


def render_escalation(path: str, months: list[dict]) -> None:
    """Line of escalation rate per close month, the headline metric."""
    periods = [month["period"] for month in months]
    rates = [month["escalation_rate"] for month in months]
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    ax.plot(periods, rates, marker="o", linewidth=2)
    ax.set_xlabel("Close month")
    ax.set_ylabel("Escalation rate")
    ax.set_title("Escalation rate by close month")
    ax.set_ylim(0, max(rates + [0.01]) * 1.25)
    ax.grid(True, axis="y", alpha=0.3)
    for period, rate in zip(periods, rates, strict=True):
        ax.annotate(
            f"{rate:.2f}",
            (period, rate),
            textcoords="offset points",
            xytext=(0, 8),
            ha="center",
        )
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def render_f1_by_category(path: str, by_category: dict[str, dict]) -> None:
    """Horizontal bars of F1 per anomaly category across all months."""
    items = sorted(by_category.items(), key=lambda item: (item[1]["f1"], item[0]))
    labels = [category for category, _ in items]
    values = [metrics["f1"] for _, metrics in items]
    fig, ax = plt.subplots(figsize=(7.2, 0.45 * max(len(items), 4) + 1.6))
    ax.barh(labels, values)
    ax.set_xlabel("F1 score")
    ax.set_ylabel("Category")
    ax.set_title("Detection F1 by category (all months)")
    ax.set_xlim(0, 1.05)
    ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
