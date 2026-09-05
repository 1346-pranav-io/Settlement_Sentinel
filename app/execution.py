"""
Mock core settlement API.

Stands in for Razorpay's real payout/refund/dispute settlement APIs. The
two properties that matter for the sentinel's guarantees live here:

1. Idempotency: the same idempotency_key always returns the same tx_id and
   never executes twice, even under retries or crashes-and-resubmits.
2. Reversal window: an executed transaction can be reversed up until its
   reversal_deadline, after which it's considered finally settled.
"""
from __future__ import annotations

import time
import uuid

try:
    from app.models import ExecutionResult, SettlementIntent
except ImportError:
    from .models import ExecutionResult, SettlementIntent  # type: ignore

REVERSAL_WINDOW_SECONDS = 300  # 5 minutes for this demo; real system would vary by rail


class SettlementExecutor:
    def __init__(self):
        self._by_idempotency_key: dict[str, ExecutionResult] = {}
        self._reversed: set[str] = set()

    def execute(self, intent: SettlementIntent) -> ExecutionResult:
        existing = self._by_idempotency_key.get(intent.idempotency_key)
        if existing is not None:
            # Same key seen before - return the original result, do NOT re-execute.
            return ExecutionResult(
                tx_id=existing.tx_id,
                intent_id=existing.intent_id,
                executed=True,
                idempotent_replay=True,
                reversal_deadline=existing.reversal_deadline,
                executed_at=existing.executed_at,
            )

        now = time.time()
        result = ExecutionResult(
            tx_id=f"tx_{uuid.uuid4().hex[:14]}",
            intent_id=intent.intent_id,
            executed=True,
            idempotent_replay=False,
            reversal_deadline=now + REVERSAL_WINDOW_SECONDS,
            executed_at=now,
        )
        self._by_idempotency_key[intent.idempotency_key] = result
        return result

    def reverse(self, tx_id: str) -> dict:
        for result in self._by_idempotency_key.values():
            if result.tx_id == tx_id:
                if tx_id in self._reversed:
                    return {"reversed": False, "reason": "already reversed"}
                if time.time() > result.reversal_deadline:
                    return {"reversed": False, "reason": "reversal window has closed"}
                self._reversed.add(tx_id)
                return {"reversed": True, "reason": None}
        return {"reversed": False, "reason": "tx_id not found"}
