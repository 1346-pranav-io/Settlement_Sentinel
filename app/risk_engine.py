"""
Deterministic risk rules engine.

This is the fast path: no LLM calls, pure rules, sub-millisecond in practice.
Its job is to cheaply clear the ~90% of settlement intents that are obviously
fine, and flag the rest for a second opinion from the judge agent. It never
approves anything by itself for actions above the auto-approve ceiling -
approval always requires either passing every rule cleanly and being under
the ceiling, or a downstream judge/human sign-off.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

try:
    from app.models import ActionType, RiskAssessment, SettlementIntent
except ImportError:
    from .models import ActionType, RiskAssessment, SettlementIntent  # type: ignore

# --- Configurable thresholds -------------------------------------------------

AUTO_APPROVE_CEILING = {
    ActionType.REFUND: 5_000.0,
    ActionType.PAYOUT: 10_000.0,
    ActionType.DISPUTE_RESPONSE: 0.0,  # disputes always get a second opinion
    ActionType.LEDGER_ADJUSTMENT: 1_000.0,
}

JUDGE_RISK_SCORE_THRESHOLD = 30
VELOCITY_WINDOW_SECONDS = 60
VELOCITY_MAX_ACTIONS = 5
MIN_AGENT_CONFIDENCE = 0.7


class RiskRulesEngine:
    def __init__(self, denylist: set[str] | None = None):
        self.denylist = denylist or set()
        # merchant_id -> deque of recent action timestamps, for velocity checks
        self._velocity: dict[str, deque] = defaultdict(deque)

    def evaluate(self, intent: SettlementIntent) -> RiskAssessment:
        flags: list[str] = []
        score = 0

        # Hard block: denylisted target entity. This skips the judge entirely
        # and goes straight to human escalation - we don't let an LLM talk us
        # out of a denylist hit.
        if intent.target_entity in self.denylist:
            return RiskAssessment(
                risk_score=100,
                flags=["denylisted_entity"],
                requires_judge=False,
                hard_block=True,
                hard_block_reason=f"target_entity '{intent.target_entity}' is denylisted",
            )

        # Amount vs. auto-approve ceiling for this action type
        ceiling = AUTO_APPROVE_CEILING.get(intent.action_type, 0.0)
        if intent.amount > ceiling:
            flags.append("above_auto_approve_ceiling")
            score += 40

        # Velocity: how many actions has this merchant's agents fired recently
        now = time.time()
        window = self._velocity[intent.merchant_id]
        while window and now - window[0] > VELOCITY_WINDOW_SECONDS:
            window.popleft()
        window.append(now)
        if len(window) > VELOCITY_MAX_ACTIONS:
            flags.append("high_velocity")
            score += 30

        # Agent's own reported confidence
        if intent.agent_confidence < MIN_AGENT_CONFIDENCE:
            flags.append("low_agent_confidence")
            score += 25

        # Large single amounts always add some score, scaled
        if intent.amount > 50_000:
            flags.append("very_large_amount")
            score += 20

        # Dispute responses are inherently higher scrutiny
        if intent.action_type == ActionType.DISPUTE_RESPONSE:
            flags.append("dispute_response_always_reviewed")
            score += 15

        requires_judge = score >= JUDGE_RISK_SCORE_THRESHOLD or "low_agent_confidence" in flags

        return RiskAssessment(
            risk_score=min(score, 99),
            flags=flags,
            requires_judge=requires_judge,
            hard_block=False,
        )
