"""
Simulated autonomous agents.

These stand in for real Agent Studio agents (a dispute-resolution agent, a
failed-payment-recovery agent, etc). Each one just decides it wants to take
a financial action and builds a SettlementIntent describing why - exactly
the shape a real Claude Agent SDK agent would produce. They never call the
executor directly; everything goes through Sentinel.submit().
"""
from __future__ import annotations

import uuid

from app.models import ActionType, SettlementIntent


def _idempotency_key() -> str:
    return f"idem_{uuid.uuid4().hex[:12]}"


class RefundRecoveryAgent:
    """Watches failed/disputed payments and issues refunds it's confident about."""

    agent_id = "agent_refund_recovery_v1"

    def routine_refund(self, merchant_id: str, customer_id: str, amount: float) -> SettlementIntent:
        return SettlementIntent(
            action_type=ActionType.REFUND,
            amount=amount,
            merchant_id=merchant_id,
            target_entity=customer_id,
            agent_id=self.agent_id,
            agent_reasoning=(
                f"Customer {customer_id} was double-charged due to a gateway retry; "
                f"matched duplicate transaction IDs in the last 10 minutes, refunding the duplicate."
            ),
            agent_confidence=0.95,
            idempotency_key=_idempotency_key(),
        )

    def uncertain_refund(self, merchant_id: str, customer_id: str, amount: float) -> SettlementIntent:
        return SettlementIntent(
            action_type=ActionType.REFUND,
            amount=amount,
            merchant_id=merchant_id,
            target_entity=customer_id,
            agent_id=self.agent_id,
            agent_reasoning=(
                "Customer complained about a failed order. I could not fully verify "
                "the order status against the merchant's inventory system, but the "
                "complaint pattern looks similar to past valid refund cases so I'll guess this is legitimate."
            ),
            agent_confidence=0.4,
            idempotency_key=_idempotency_key(),
        )


class PayoutAgent:
    """Runs scheduled/on-demand payouts to merchant bank accounts."""

    agent_id = "agent_payout_v1"

    def large_payout(self, merchant_id: str, bank_account: str, amount: float) -> SettlementIntent:
        return SettlementIntent(
            action_type=ActionType.PAYOUT,
            amount=amount,
            merchant_id=merchant_id,
            target_entity=bank_account,
            agent_id=self.agent_id,
            agent_reasoning=(
                f"Weekly settlement payout for merchant {merchant_id} based on "
                f"reconciled transaction volume for the last 7 days."
            ),
            agent_confidence=0.9,
            idempotency_key=_idempotency_key(),
        )

    def payout_to_denylisted_account(self, merchant_id: str, bank_account: str, amount: float) -> SettlementIntent:
        return SettlementIntent(
            action_type=ActionType.PAYOUT,
            amount=amount,
            merchant_id=merchant_id,
            target_entity=bank_account,
            agent_id=self.agent_id,
            agent_reasoning="Standard payout run, account matched from merchant's saved bank details.",
            agent_confidence=0.9,
            idempotency_key=_idempotency_key(),
        )


class DisputeResponseAgent:
    """Responds to card-network chargebacks with evidence."""

    agent_id = "agent_dispute_response_v1"

    def submit_evidence(self, merchant_id: str, dispute_id: str, amount: float) -> SettlementIntent:
        return SettlementIntent(
            action_type=ActionType.DISPUTE_RESPONSE,
            amount=amount,
            merchant_id=merchant_id,
            target_entity=dispute_id,
            agent_id=self.agent_id,
            agent_reasoning=(
                f"Dispute {dispute_id}: found matching delivery confirmation and "
                f"customer signature on file, submitting as compelling evidence to contest chargeback."
            ),
            agent_confidence=0.85,
            idempotency_key=_idempotency_key(),
        )
