"""Synthetic ERP data for the Ledger Sentinel demo company.

Rebuild the database and the planted-anomaly ground truth with:

    uv run python -m sentinel.datagen --db data/sentinel.db --seed 42

The same seed always produces the same rows and the same ground truth file.
See generator.py for the ground truth row conventions shared with the
engines and the eval harness.
"""

from sentinel.datagen.generator import ANOMALY_CATEGORIES, generate

__all__ = ["ANOMALY_CATEGORIES", "generate"]
