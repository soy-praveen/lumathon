"""Pydantic models shared across engines.

The ProposedJE validators are the product's core guarantee: no journal entry
exists without evidence, and every entry balances.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

BALANCE_TOLERANCE = 0.005  # half a cent


class Evidence(BaseModel):
    source_table: str
    row_id: int
    note: str | None = None


class JELine(BaseModel):
    account: str
    debit: float = 0.0
    credit: float = 0.0


class ProposedJE(BaseModel):
    lines: list[JELine]
    evidence: list[Evidence]
    rule: str
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["auto_approved", "needs_review", "approved", "rejected"]

    @field_validator("evidence")
    @classmethod
    def evidence_must_be_non_empty(cls, value: list[Evidence]) -> list[Evidence]:
        if not value:
            raise ValueError("a proposed JE must cite at least one evidence row")
        return value

    @field_validator("lines")
    @classmethod
    def lines_must_be_non_empty(cls, value: list[JELine]) -> list[JELine]:
        if not value:
            raise ValueError("a proposed JE must have at least one line")
        return value

    @model_validator(mode="after")
    def lines_must_balance(self) -> ProposedJE:
        debits = sum(line.debit for line in self.lines)
        credits = sum(line.credit for line in self.lines)
        if abs(debits - credits) > BALANCE_TOLERANCE:
            raise ValueError(f"JE does not balance: debits {debits:.2f} != credits {credits:.2f}")
        return self


class PolicyRule(BaseModel):
    scope: str
    condition: str
    action: str
    limits: dict | None = None
    active: bool = True
    expires: str | None = None  # YYYY-MM, inclusive last period the rule applies
