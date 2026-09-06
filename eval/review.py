"""Simulated human review loop and the deterministic rule-distillation fallback.

Between months the harness plays the reviewer: every needs_review JE and open
exception is resolved against data/ground_truth.json, approving what
corresponds to a planted anomaly and rejecting the rest, always with a stated
reason. Approved exceptions then feed the learning loop through
sentinel.agent.rules; when the LLM is unavailable the harness distills a
deterministic template rule from the exception's own fields instead.
"""

from __future__ import annotations

import re

from sqlalchemy.engine import Engine

from eval.scoring import EXCEPTION_CATEGORY_ALIASES, SCORED_JE_RULES
from sentinel import llm
from sentinel.agent import resolution
from sentinel.agent import rules as agent_rules
from sentinel.db import (
    APInvoice,
    AuditLog,
    BankLine,
    ExceptionRecord,
    GLEntry,
    PolicyRuleRow,
    ProposedJERow,
    Vendor,
    get_session,
)
from sentinel.recon.accounts import resolve_accounts
from sentinel.schemas import PolicyRule

REVIEW_ACTOR = "eval_reviewer"

# Anomaly exceptions name their partner rows in the description; a split
# detection points at one invoice of the band and lists the rest here.
_PARTNER_ROWS = re.compile(r"partner ap_invoices rows? ([0-9][0-9, ]*)")


def load_ground_truth_index(engine: Engine, ground_truth: dict[str, list[dict]]) -> list[dict]:
    """Flatten the per-period ground truth into rows with acceptable targets.

    Most rows accept exactly the (source_table, row_id) they were planted
    with. vendor_name_variance is bridged: the generator points at the
    ap_invoices row booked to the variant name while the anomaly engine
    points at the variant vendors row, so both rows are acceptable.
    """
    index: list[dict] = []
    with get_session(engine) as session:
        for period, rows in ground_truth.items():
            for row in rows:
                targets = {(row["source_table"], row["row_id"])}
                if row["category"] == "vendor_name_variance" and row["source_table"] == (
                    "ap_invoices"
                ):
                    invoice = session.get(APInvoice, row["row_id"])
                    if invoice is not None:
                        targets.add(("vendors", invoice.vendor_id))
                index.append(
                    {
                        "period": period,
                        "category": row["category"],
                        "targets": targets,
                        "description": row["description"],
                    }
                )
    return index


def detection_targets(source_table: str | None, row_id: int | None, description: str) -> set:
    """Rows a detection points at: its own row plus partners it names."""
    targets: set = set()
    if source_table is not None and row_id is not None:
        targets.add((source_table, row_id))
    for match in _PARTNER_ROWS.finditer(description or ""):
        for token in match.group(1).split(","):
            token = token.strip()
            if token:
                targets.add(("ap_invoices", int(token)))
    return targets


def find_planted(gt_index: list[dict], claimed: set[str], targets: set) -> dict | None:
    """First ground truth row satisfied by a detection's claim and targets."""
    for gt_row in gt_index:
        if gt_row["category"] in claimed and targets & gt_row["targets"]:
            return gt_row
    return None


def review_period(engine: Engine, period: str, gt_index: list[dict]) -> dict:
    """Resolve the period's needs_review JEs and open exceptions.

    Approves what corresponds to a planted anomaly, rejects the rest, and
    returns counts plus the ids of approved exceptions for the learning loop.
    """
    counts = {
        "jes_approved": 0,
        "jes_rejected": 0,
        "exceptions_approved": 0,
        "exceptions_rejected": 0,
    }
    approved_exception_ids: list[int] = []

    with get_session(engine) as session:
        je_rows = (
            session.query(ProposedJERow)
            .filter(ProposedJERow.period == period, ProposedJERow.status == "needs_review")
            .order_by(ProposedJERow.id)
            .all()
        )
        for row in je_rows:
            targets = {(item["source_table"], item["row_id"]) for item in row.evidence_json}
            claimed = {row.rule} if row.rule in SCORED_JE_RULES else set()
            planted = find_planted(gt_index, claimed, targets)
            if planted is not None:
                row.status = "approved"
                reason = (
                    f"Matches planted {planted['category']} anomaly: "
                    f"{planted['description']}"
                )[:500]
            else:
                row.status = "rejected"
                reason = (
                    "Cited rows do not correspond to any planted anomaly; "
                    "rejected as a false positive."
                )
            counts["jes_approved" if row.status == "approved" else "jes_rejected"] += 1
            session.add(
                AuditLog(
                    actor=REVIEW_ACTOR,
                    action="je.reviewed",
                    detail={"je_id": row.id, "period": period, "status": row.status,
                            "reason": reason},
                )
            )

        open_exceptions = [
            (record.id, record.category, record.source_table, record.row_id, record.description)
            for record in session.query(ExceptionRecord)
            .filter(ExceptionRecord.period == period, ExceptionRecord.status == "open")
            .order_by(ExceptionRecord.id)
            .all()
        ]

    for exc_id, category, source_table, row_id, description in open_exceptions:
        targets = detection_targets(source_table, row_id, description)
        claimed = {category} | set(EXCEPTION_CATEGORY_ALIASES.get(category, frozenset()))
        planted = find_planted(gt_index, claimed, targets)
        if planted is not None:
            reason = (
                f"Confirmed planted {planted['category']} anomaly: {planted['description']}"
            )[:500]
            resolution.resolve_exception(engine, exc_id, "approve", reason, REVIEW_ACTOR)
            counts["exceptions_approved"] += 1
            approved_exception_ids.append(exc_id)
        else:
            reason = (
                "Checked the cited source rows; no planted anomaly here, "
                "the flagged pattern is routine activity."
            )
            resolution.resolve_exception(engine, exc_id, "reject", reason, REVIEW_ACTOR)
            counts["exceptions_rejected"] += 1

    return {"counts": counts, "approved_exception_ids": approved_exception_ids}


def learn_from_period(
    engine: Engine, approved_exception_ids: list[int], closed_periods: list[str]
) -> dict:
    """Distill and promote one policy rule per approved exception.

    Tries the LLM first; on LLMError falls back to a deterministic template
    built from the exception's fields. Promotion always goes through the
    agent.rules regression gate against every closed period.
    """
    stats = {"rules_distilled": 0, "rules_promoted": 0, "rules_gate_rejected": 0}
    for exc_id in approved_exception_ids:
        try:
            rule = agent_rules.distill_rule(engine, exc_id)
        except llm.LLMError:
            rule = template_rule(engine, exc_id)
        if rule is None or _rule_exists(engine, rule):
            continue
        stats["rules_distilled"] += 1
        outcome = agent_rules.promote_rule(engine, rule, exc_id, closed_periods)
        if outcome.promoted:
            stats["rules_promoted"] += 1
        else:
            stats["rules_gate_rejected"] += 1
    return stats


def template_rule(engine: Engine, exception_id: int) -> PolicyRule | None:
    """Deterministic fallback distillation for recon_unmatched exceptions.

    A bank-side exception yields a descriptor prefix rule; a GL-side timing
    exception yields a substring rule on the vendor's leading name token
    (bank descriptors carry the vendor name, e.g. FIGMA MONTHLY for Figma).
    Both book to the clearing account with an amount cap, mirroring the
    recon engine's own timing treatment. Other categories have no safe
    template, so the exception simply stays a resolved human decision.
    """
    with get_session(engine) as session:
        record = session.get(ExceptionRecord, exception_id)
        if record is None or record.category != "recon_unmatched":
            return None
        clearing = resolve_accounts(session)["clearing"]
        if record.source_table == "bank_lines":
            line = session.get(BankLine, record.row_id)
            if line is None:
                return None
            tokens = line.descriptor.split()
            prefix = " ".join(tokens[:2]) if len(tokens) >= 2 else line.descriptor
            condition = f"prefix:{prefix}"
            cap = round(abs(line.amount) * 1.5, 2)
        elif record.source_table == "gl_entries":
            entry = session.get(GLEntry, record.row_id)
            if entry is None:
                return None
            vendor = _vendor_named_in(session, entry.description)
            if vendor is None:
                return None
            condition = f"substring:{vendor.name.split()[0]}"
            cap = round(max(entry.debit, entry.credit) * 1.25, 2)
        else:
            return None
    return PolicyRule(
        scope="bank",
        condition=condition,
        action=f"book_to:{clearing}",
        limits={"max_amount": cap},
    )


def _vendor_named_in(session, text: str) -> Vendor | None:
    """Longest vendor name contained in the text, if any."""
    lowered = (text or "").lower()
    best: Vendor | None = None
    for vendor in session.query(Vendor).order_by(Vendor.id).all():
        if vendor.name.lower() in lowered:
            if best is None or len(vendor.name) > len(best.name):
                best = vendor
    return best


def _rule_exists(engine: Engine, rule: PolicyRule) -> bool:
    with get_session(engine) as session:
        return (
            session.query(PolicyRuleRow)
            .filter(
                PolicyRuleRow.scope == rule.scope,
                PolicyRuleRow.condition == rule.condition,
                PolicyRuleRow.action == rule.action,
                PolicyRuleRow.active.is_(True),
            )
            .first()
            is not None
        )
