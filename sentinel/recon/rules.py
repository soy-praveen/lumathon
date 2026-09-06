"""Policy rule application for bank reconciliation.

Rule grammar (all descriptor matching is case-insensitive):

Scope
    The rule applies to reconciliation when the scope contains ``bank`` or
    ``recon`` (for example ``bank``, ``bank_descriptor``, ``recon``).

Condition, matched against the raw bank descriptor
    ``substring:<text>``  the descriptor contains <text>
    ``prefix:<text>``     the descriptor starts with <text>
    Anything without a recognized marker is treated as ``substring:<text>``.

Action
    ``book_to:<account_code>``  propose a JE booking the line against cash to
    that account. Rules with any other action are ignored by this engine.

Limits (optional dict)
    ``{"max_amount": <number>}``  skip lines whose absolute amount exceeds it.

Expiry
    ``expires`` is a YYYY-MM period, inclusive; the rule is skipped for later
    periods. Inactive rules are skipped.

A matched rule yields a ``rule`` match record and a ProposedJE citing the rule
in its ``rule`` field. Rule matches auto-approve at a lower confidence
threshold than heuristic proposals because a human promoted the rule.
"""

from __future__ import annotations

from sentinel.db import BankLine
from sentinel.schemas import Evidence, JELine, PolicyRule, ProposedJE

RULE_MATCH_CONFIDENCE = 0.75
RULE_AUTO_APPROVE_THRESHOLD = 0.7


def apply_rules(
    bank_lines: list[BankLine],
    rules: list[PolicyRule],
    period: str,
    accounts: dict[str, str],
) -> tuple[list[dict], list[ProposedJE], list[BankLine]]:
    """Apply policy rules to unmatched bank lines.

    Returns (matches, proposed_jes, still_unmatched).
    """
    active = [rule for rule in rules if _applies_to_recon(rule, period)]
    matches: list[dict] = []
    jes: list[ProposedJE] = []
    remaining: list[BankLine] = []
    for line in bank_lines:
        rule = next((r for r in active if _rule_hits(r, line)), None)
        if rule is None:
            remaining.append(line)
            continue
        matches.append(
            {
                "bank_line_ids": [line.id],
                "gl_entry_ids": [],
                "match_type": "rule",
                "confidence": RULE_MATCH_CONFIDENCE,
            }
        )
        jes.append(_rule_je(rule, line, accounts))
    return matches, jes, remaining


def _applies_to_recon(rule: PolicyRule, period: str) -> bool:
    if not rule.active:
        return False
    if rule.expires is not None and period > rule.expires:
        return False
    scope = rule.scope.lower()
    if "bank" not in scope and "recon" not in scope:
        return False
    return _action_account(rule) is not None


def _rule_hits(rule: PolicyRule, line: BankLine) -> bool:
    if not _condition_matches(rule.condition, line.descriptor):
        return False
    limits = rule.limits or {}
    max_amount = limits.get("max_amount")
    if max_amount is not None and abs(line.amount) > float(max_amount):
        return False
    return True


def _condition_matches(condition: str, descriptor: str) -> bool:
    cond = condition.strip()
    lowered = descriptor.lower()
    if cond.lower().startswith("prefix:"):
        return lowered.startswith(cond[len("prefix:") :].strip().lower())
    if cond.lower().startswith("substring:"):
        cond = cond[len("substring:") :]
    return cond.strip().lower() in lowered


def _action_account(rule: PolicyRule) -> str | None:
    action = rule.action.strip()
    if action.lower().startswith("book_to:"):
        code = action[len("book_to:") :].strip()
        return code or None
    return None


def _rule_je(rule: PolicyRule, line: BankLine, accounts: dict[str, str]) -> ProposedJE:
    account = _action_account(rule)
    amount = round(abs(line.amount), 2)
    if line.amount < 0:
        lines = [
            JELine(account=account, debit=amount),
            JELine(account=accounts["cash"], credit=amount),
        ]
    else:
        lines = [
            JELine(account=accounts["cash"], debit=amount),
            JELine(account=account, credit=amount),
        ]
    status = "auto_approved" if RULE_MATCH_CONFIDENCE >= RULE_AUTO_APPROVE_THRESHOLD else (
        "needs_review"
    )
    return ProposedJE(
        lines=lines,
        evidence=[Evidence(source_table="bank_lines", row_id=line.id, note=line.descriptor)],
        rule=f"policy:{rule.scope}:{rule.condition}",
        reason=(
            f"Policy rule ({rule.condition} -> {rule.action}) matched bank "
            f"descriptor '{line.descriptor}'"
        ),
        confidence=RULE_MATCH_CONFIDENCE,
        status=status,
    )
