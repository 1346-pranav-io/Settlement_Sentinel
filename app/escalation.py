"""
Human escalation queue.

Anything hard-blocked by the risk engine, or where the judge says
"escalate" (or disagrees with the agent), lands here instead of executing.
Nothing in this queue has moved any money yet - resolving an item either
triggers execution via the sentinel or closes it as rejected.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .models import JudgeVerdict, RiskAssessment, SettlementIntent


@dataclass
class EscalationItem:
    escalation_id: str
    intent: SettlementIntent
    risk_assessment: RiskAssessment
    judge_verdict: JudgeVerdict | None
    status: str = "pending"  # pending | approved | rejected
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    resolver: str | None = None
    resolution_note: str | None = None


class EscalationQueue:
    def __init__(self):
        self._items: dict[str, EscalationItem] = {}

    def add(
        self,
        intent: SettlementIntent,
        risk_assessment: RiskAssessment,
        judge_verdict: JudgeVerdict | None,
    ) -> EscalationItem:
        item = EscalationItem(
            escalation_id=f"esc_{uuid.uuid4().hex[:10]}",
            intent=intent,
            risk_assessment=risk_assessment,
            judge_verdict=judge_verdict,
        )
        self._items[item.escalation_id] = item
        return item

    def get(self, escalation_id: str) -> EscalationItem | None:
        return self._items.get(escalation_id)

    def pending(self) -> list[EscalationItem]:
        return [i for i in self._items.values() if i.status == "pending"]

    def resolve(self, escalation_id: str, approve: bool, resolver: str, note: str = "") -> EscalationItem:
        item = self._items[escalation_id]
        item.status = "approved" if approve else "rejected"
        item.resolved_at = time.time()
        item.resolver = resolver
        item.resolution_note = note
        return item
