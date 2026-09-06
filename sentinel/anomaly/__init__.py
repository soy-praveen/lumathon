"""Anomaly detection engine: duplicate invoices, vendor name variance, splits, out-of-period."""

from sentinel.anomaly.engine import AnomalyResult, run_anomaly

__all__ = ["AnomalyResult", "run_anomaly"]
