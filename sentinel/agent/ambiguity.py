"""LLM triage for proposed JEs whose confidence falls in the ambiguity band.

The model only ever adjusts the confidence or status of an existing,
evidence-backed proposal: lines and evidence are copied through unchanged, so
it is structurally impossible for the LLM to invent a journal entry. Any LLM
failure degrades to escalation (needs_review) instead of blocking the close.

Thresholds live in sentinel.agent.thresholds.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from sentinel import llm
from sentinel.agent import thresholds
from sentinel.schemas import ProposedJE

_SYSTEM = (
    "You are a cautious accounting reviewer inside a month-end close agent. "
    "You may only accept, reject, or escalate an existing evidence-backed "
    "journal entry proposal, and optionally adjust its confidence. You never "
    "draft new entries, lines, or evidence. When unsure, escalate."
)

_REASON_MAX = 500  # matches the proposed_jes.reason column width


class AmbiguityVerdict(BaseModel):
    """Small verdict model the LLM must fill for an ambiguous JE."""

    verdict: Literal["accept", "reject", "escalate"]
    adjusted_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str = ""


def triage_jes(jes: Iterable[ProposedJE]) -> list[ProposedJE]:
    """Triage every proposed JE per the documented confidence bands."""
    return [triage_je(je) for je in jes]


def triage_je(je: ProposedJE) -> ProposedJE:
    """Return the JE with its post-triage status, calling the LLM only in band."""
    rule_backed = thresholds.is_rule_backed(je)
    if je.confidence >= thresholds.auto_approve_threshold(rule_backed):
        return je
    if not thresholds.in_ambiguity_band(je.confidence, rule_backed):
        return _updated(je, status="needs_review", note="below escalation floor")
    return review_je(je)


def review_je(je: ProposedJE) -> ProposedJE:
    """Ask the LLM for a verdict on one in-band JE; escalate on any LLM failure."""
    try:
        verdict = llm.complete_json(_build_prompt(je), AmbiguityVerdict, system=_SYSTEM)
    except llm.LLMError as exc:
        return _updated(je, status="needs_review", note=f"llm error, escalated: {exc}")
    confidence = je.confidence
    if verdict.adjusted_confidence is not None:
        confidence = verdict.adjusted_confidence
    note = f"llm {verdict.verdict}: {verdict.rationale}".strip()
    status_by_verdict = {
        "accept": "auto_approved",
        "reject": "rejected",
        "escalate": "needs_review",
    }
    return _updated(je, status=status_by_verdict[verdict.verdict], confidence=confidence, note=note)


def _build_prompt(je: ProposedJE) -> str:
    return (
        "Review this proposed journal entry from a month-end close. Decide "
        "whether to accept it as is, reject it, or escalate it to a human "
        "reviewer.\n\n"
        f"{json.dumps(je.model_dump(), indent=2)}\n\n"
        "Accept only when the evidence clearly supports the entry. Reject only "
        "when it is clearly wrong. Otherwise escalate. You may adjust the "
        "confidence; you may not change anything else."
    )


def _updated(
    je: ProposedJE,
    status: str,
    confidence: float | None = None,
    note: str = "",
) -> ProposedJE:
    reason = f"{je.reason} [{note}]" if note else je.reason
    update: dict = {"status": status, "reason": reason[:_REASON_MAX]}
    if confidence is not None:
        update["confidence"] = confidence
    return je.model_copy(update=update)
