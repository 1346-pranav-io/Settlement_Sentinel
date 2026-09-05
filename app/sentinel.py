"""
Settlement Sentinel: the orchestrator.

This is the single choke point every agent-initiated settlement action must
pass through. It never lets an agent talk directly to the executor - every
path goes: risk engine -> (maybe) judge -> execute OR escalate, and every
outcome is written to the audit ledger before the function returns.

Guiding rule baked into the control flow below: FAIL CLOSED. Any exception,
timeout, or disagreement between the agent/judge/risk-engine routes to human
escalation rather than defaulting to execution.
"""
from __future__ import annotations

try:
    from app.escalation import EscalationQueue
    from app.execution import SettlementExecutor
    from app.judge_agent import JudgeAgent
    from app.ledger import AuditLedger
    from app.models import (
        Decision,
        RiskAssessment,
        SentinelResponse,
        SettlementIntent,
        Verdict,
    )
    from app.risk_engine import RiskRulesEngine
except ImportError:
    from .escalation import EscalationQueue  # type: ignore
    from .execution import SettlementExecutor  # type: ignore
    from .judge_agent import JudgeAgent  # type: ignore
    from .ledger import AuditLedger  # type: ignore
    from .models import (  # type: ignore
        Decision,
        RiskAssessment,
        SentinelResponse,
        SettlementIntent,
        Verdict,
    )
    from .risk_engine import RiskRulesEngine  # type: ignore


class SettlementSentinel:
    def __init__(
        self,
        risk_engine: RiskRulesEngine | None = None,
        judge: JudgeAgent | None = None,
        executor: SettlementExecutor | None = None,
        ledger: AuditLedger | None = None,
        escalation_queue: EscalationQueue | None = None,
    ):
        self.risk_engine = risk_engine or RiskRulesEngine()
        self.judge = judge or JudgeAgent()
        self.executor = executor or SettlementExecutor()
        self.ledger = ledger or AuditLedger()
        self.escalation_queue = escalation_queue or EscalationQueue()

    def submit(self, intent: SettlementIntent) -> SentinelResponse:
        risk = self.risk_engine.evaluate(intent)

        # 1. Hard block -> straight to human, never touches the judge or executor.
        if risk.hard_block:
            item = self.escalation_queue.add(intent, risk, judge_verdict=None)
            self._log(intent, Decision(
                intent_id=intent.intent_id,
                verdict=Verdict.ESCALATED,
                risk_assessment=risk,
                note=risk.hard_block_reason,
            ))
            return SentinelResponse(
                intent_id=intent.intent_id,
                verdict=Verdict.ESCALATED,
                risk_score=risk.risk_score,
                flags=risk.flags,
                escalation_id=item.escalation_id,
                message=f"Hard blocked: {risk.hard_block_reason}",
            )

        # 2. Clean and low-risk -> auto-approve and execute immediately.
        if not risk.requires_judge:
            exec_result = self.executor.execute(intent)
            self._log(intent, Decision(
                intent_id=intent.intent_id,
                verdict=Verdict.AUTO_APPROVED,
                risk_assessment=risk,
            ), exec_result)
            return SentinelResponse(
                intent_id=intent.intent_id,
                verdict=Verdict.AUTO_APPROVED,
                risk_score=risk.risk_score,
                flags=risk.flags,
                execution=exec_result,
                message="Auto-approved by risk rules engine, executed.",
            )

        # 3. Flagged -> independent judge review. Fail-closed is enforced
        #    inside JudgeAgent.review() itself, so any judge failure already
        #    comes back to us as decision="escalate".
        judge_verdict = self.judge.review(intent, risk)

        if judge_verdict.decision == "approve":
            exec_result = self.executor.execute(intent)
            self._log(intent, Decision(
                intent_id=intent.intent_id,
                verdict=Verdict.JUDGE_APPROVED,
                risk_assessment=risk,
                judge_verdict=judge_verdict,
            ), exec_result)
            return SentinelResponse(
                intent_id=intent.intent_id,
                verdict=Verdict.JUDGE_APPROVED,
                risk_score=risk.risk_score,
                flags=risk.flags,
                judge_verdict=judge_verdict,
                execution=exec_result,
                message="Approved by independent judge, executed.",
            )

        if judge_verdict.decision == "reject":
            self._log(intent, Decision(
                intent_id=intent.intent_id,
                verdict=Verdict.JUDGE_REJECTED,
                risk_assessment=risk,
                judge_verdict=judge_verdict,
            ))
            return SentinelResponse(
                intent_id=intent.intent_id,
                verdict=Verdict.JUDGE_REJECTED,
                risk_score=risk.risk_score,
                flags=risk.flags,
                judge_verdict=judge_verdict,
                message="Rejected by independent judge, not executed.",
            )

        # decision == "escalate" (or anything unexpected - treated as escalate)
        item = self.escalation_queue.add(intent, risk, judge_verdict)
        self._log(intent, Decision(
            intent_id=intent.intent_id,
            verdict=Verdict.ESCALATED,
            risk_assessment=risk,
            judge_verdict=judge_verdict,
        ))
        return SentinelResponse(
            intent_id=intent.intent_id,
            verdict=Verdict.ESCALATED,
            risk_score=risk.risk_score,
            flags=risk.flags,
            judge_verdict=judge_verdict,
            escalation_id=item.escalation_id,
            message="Escalated to human review.",
        )

    def resolve_escalation(self, escalation_id: str, approve: bool, resolver: str, note: str = "") -> SentinelResponse:
        item = self.escalation_queue.resolve(escalation_id, approve, resolver, note)
        if approve:
            exec_result = self.executor.execute(item.intent)
            self._log(item.intent, Decision(
                intent_id=item.intent.intent_id,
                verdict=Verdict.HUMAN_APPROVED,
                risk_assessment=item.risk_assessment,
                judge_verdict=item.judge_verdict,
                note=note,
            ), exec_result)
            return SentinelResponse(
                intent_id=item.intent.intent_id,
                verdict=Verdict.HUMAN_APPROVED,
                risk_score=item.risk_assessment.risk_score,
                flags=item.risk_assessment.flags,
                execution=exec_result,
                message=f"Human ({resolver}) approved, executed.",
            )
        self._log(item.intent, Decision(
            intent_id=item.intent.intent_id,
            verdict=Verdict.HUMAN_REJECTED,
            risk_assessment=item.risk_assessment,
            judge_verdict=item.judge_verdict,
            note=note,
        ))
        return SentinelResponse(
            intent_id=item.intent.intent_id,
            verdict=Verdict.HUMAN_REJECTED,
            risk_score=item.risk_assessment.risk_score,
            flags=item.risk_assessment.flags,
            message=f"Human ({resolver}) rejected, not executed.",
        )

    def reverse(self, tx_id: str, reason: str, requested_by: str) -> dict:
        result = self.executor.reverse(tx_id)
        self.ledger.append({
            "type": "reversal",
            "tx_id": tx_id,
            "reason": reason,
            "requested_by": requested_by,
            "result": result,
        })
        return result

    def _log(self, intent: SettlementIntent, decision: Decision, exec_result=None) -> None:
        self.ledger.append({
            "type": "decision",
            "intent": intent.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "execution": exec_result.model_dump(mode="json") if exec_result else None,
        })
