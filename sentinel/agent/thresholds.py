"""Escalation thresholds for the close agent. This is the only place they live.

Triage bands, applied to ProposedJE.confidence:

- confidence at or above the auto-approve threshold: trust the deterministic
  engine and keep its status.
- ambiguity band, from ESCALATION_FLOOR up to (but excluding) the auto-approve
  threshold: ask the LLM to accept, reject, or escalate the proposal.
- below ESCALATION_FLOOR: escalate straight to needs_review, no LLM call.

A JE backed by a promoted policy rule uses a lower auto-approve threshold
because a prior human decision already stands behind the pattern. A JE counts
as rule-backed when its rule name carries the policy prefix or its evidence
cites a policy_rules row.
"""

from __future__ import annotations

from sentinel.schemas import ProposedJE

AUTO_APPROVE_THRESHOLD = 0.85
RULE_BACKED_AUTO_APPROVE_THRESHOLD = 0.70
ESCALATION_FLOOR = 0.50

RULE_BACKED_PREFIX = "policy:"


def is_rule_backed(je: ProposedJE) -> bool:
    """True when the JE cites a promoted policy rule."""
    if je.rule.startswith(RULE_BACKED_PREFIX):
        return True
    return any(item.source_table == "policy_rules" for item in je.evidence)


def auto_approve_threshold(rule_backed: bool) -> float:
    """Confidence needed to keep an engine's auto-approval."""
    if rule_backed:
        return RULE_BACKED_AUTO_APPROVE_THRESHOLD
    return AUTO_APPROVE_THRESHOLD


def in_ambiguity_band(confidence: float, rule_backed: bool) -> bool:
    """True when the JE should go to the LLM for a verdict."""
    return ESCALATION_FLOOR <= confidence < auto_approve_threshold(rule_backed)
