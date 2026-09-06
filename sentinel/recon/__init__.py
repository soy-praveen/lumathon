"""Bank reconciliation engine.

Deterministic matching of bank statement lines to GL cash entries, followed by
evidence-backed journal entry proposals for bank-only activity (fees, interest,
timing differences), Dodo payout reconciliation, and policy rule application.

Public API:
    run_recon(session, period, rules=None) -> ReconResult

The policy rule condition grammar is documented in sentinel.recon.rules.
"""

from sentinel.recon.engine import run_recon
from sentinel.recon.models import ReconResult

__all__ = ["ReconResult", "run_recon"]
