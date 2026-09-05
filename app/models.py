"""
Core data models for Settlement Sentinel.

These are the contracts every agent, the risk engine, the judge, the
executor, and the ledger all speak. Keeping this one file as the single
source of truth avoids drift between components.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ActionType(str, Enum):
    REFUND = "refund"
    PAYOUT = "payout"
    DISPUTE_RESPONSE = "dispute_response"
    LEDGER_ADJUSTMENT = "ledger_adjustment"


class SettlementIntent(BaseModel):
    """What an agent submits when it wants to move money or settle a dispute."""

    intent_id: str = Field(default_factory=lambda: f"int_{uuid.uuid4().hex[:12]}")
    action_type: ActionType
    amount: float
    currency: str = "INR"
    merchant_id: str
    target_entity: str  # customer id, bank account, dispute id, etc.
    agent_id: str
    agent_reasoning: str  # free-text trace of *why* the agent wants to do this
    agent_confidence: float = Field(ge=0.0, le=1.0)
    idempotency_key: str
    metadata: dict = Field(default_factory=dict)
    submitted_at: float = Field(default_factory=time.time)

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v


class RiskAssessment(BaseModel):
    risk_score: int  # 0-100
    flags: list[str] = Field(default_factory=list)
    requires_judge: bool
    hard_block: bool
    hard_block_reason: Optional[str] = None


class JudgeVerdict(BaseModel):
    decision: str  # "approve" | "reject" | "escalate"
    reasoning: str
    judge_confidence: float
    source: str  # "llm" or "heuristic-fallback"


class Verdict(str, Enum):
    AUTO_APPROVED = "auto_approved"
    JUDGE_APPROVED = "judge_approved"
    JUDGE_REJECTED = "judge_rejected"
    ESCALATED = "escalated"
    HUMAN_APPROVED = "human_approved"
    HUMAN_REJECTED = "human_rejected"
    REVERSED = "reversed"


class Decision(BaseModel):
    intent_id: str
    verdict: Verdict
    risk_assessment: RiskAssessment
    judge_verdict: Optional[JudgeVerdict] = None
    decided_at: float = Field(default_factory=time.time)
    note: Optional[str] = None


class ExecutionResult(BaseModel):
    tx_id: str
    intent_id: str
    executed: bool
    idempotent_replay: bool  # True if this returned a cached prior result
    reversal_deadline: float
    executed_at: float


class SentinelResponse(BaseModel):
    intent_id: str
    verdict: Verdict
    risk_score: int
    flags: list[str]
    judge_verdict: Optional[JudgeVerdict] = None
    execution: Optional[ExecutionResult] = None
    escalation_id: Optional[str] = None
    message: str
