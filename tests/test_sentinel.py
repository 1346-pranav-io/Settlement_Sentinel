import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.escalation import EscalationQueue
from app.execution import SettlementExecutor
from app.judge_agent import JudgeAgent
from app.ledger import AuditLedger
from app.models import ActionType, SettlementIntent
from app.risk_engine import RiskRulesEngine
from app.sentinel import SettlementSentinel


@pytest.fixture()
def sentinel(tmp_path):
    return SettlementSentinel(
        risk_engine=RiskRulesEngine(denylist={"denied_acct"}),
        judge=JudgeAgent(api_key=None),  # force heuristic fallback, deterministic for tests
        executor=SettlementExecutor(),
        ledger=AuditLedger(str(tmp_path / "ledger.db")),
        escalation_queue=EscalationQueue(),
    )


def make_intent(**overrides) -> SettlementIntent:
    defaults = dict(
        action_type=ActionType.REFUND,
        amount=100.0,
        merchant_id="merch_1",
        target_entity="cust_1",
        agent_id="agent_test",
        agent_reasoning="Verified duplicate charge against transaction log, refunding the duplicate.",
        agent_confidence=0.95,
        idempotency_key="idem_fixed_key_1",
    )
    defaults.update(overrides)
    return SettlementIntent(**defaults)


def test_low_risk_auto_approves_and_executes(sentinel):
    intent = make_intent()
    resp = sentinel.submit(intent)
    assert resp.verdict.value == "auto_approved"
    assert resp.execution is not None
    assert resp.execution.executed is True


def test_idempotent_retry_does_not_double_execute(sentinel):
    intent = make_intent(idempotency_key="idem_shared")
    r1 = sentinel.submit(intent)
    r2 = sentinel.submit(intent)
    assert r1.execution.tx_id == r2.execution.tx_id
    assert r2.execution.idempotent_replay is True


def test_denylisted_entity_hard_blocks_and_skips_judge(sentinel):
    intent = make_intent(target_entity="denied_acct", amount=50.0)
    resp = sentinel.submit(intent)
    assert resp.verdict.value == "escalated"
    assert "denylisted_entity" in resp.flags
    assert resp.judge_verdict is None  # never reached the judge
    assert resp.escalation_id is not None
    assert resp.execution is None  # money never moved


def test_hedging_reasoning_gets_rejected_by_heuristic_judge(sentinel):
    intent = make_intent(
        agent_confidence=0.4,
        agent_reasoning="Not sure about this one, going to guess it's a valid refund.",
    )
    resp = sentinel.submit(intent)
    assert resp.verdict.value == "judge_rejected"
    assert resp.execution is None


def test_judge_failure_fails_closed_to_escalation(monkeypatch, sentinel):
    def boom(self, intent, risk):
        raise RuntimeError("simulated judge outage")

    # Force the judge into the LLM path, then make that path explode.
    sentinel.judge.api_key = "fake-key-for-test"
    monkeypatch.setattr(JudgeAgent, "_review_with_llm", boom)

    intent = make_intent(amount=99_000.0)  # forces requires_judge=True
    resp = sentinel.submit(intent)
    assert resp.verdict.value == "escalated"
    assert resp.execution is None  # critically: did NOT default to approve
    assert resp.judge_verdict.source == "llm-error-failclosed"


def test_ledger_chain_is_valid_after_normal_operations(sentinel):
    sentinel.submit(make_intent(idempotency_key="a"))
    sentinel.submit(make_intent(idempotency_key="b", target_entity="denied_acct"))
    valid, error = sentinel.ledger.verify()
    assert valid is True
    assert error is None


def test_ledger_detects_tampering(sentinel, tmp_path):
    import json
    import sqlite3

    sentinel.submit(make_intent(idempotency_key="c"))
    db_path = sentinel.ledger.db_path
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT payload FROM ledger WHERE seq = 1").fetchone()
    payload = json.loads(row[0])
    payload["intent"]["amount"] = 9_999_999.0
    conn.execute("UPDATE ledger SET payload = ? WHERE seq = 1", (json.dumps(payload, sort_keys=True, separators=(",", ":")),))
    conn.commit()
    conn.close()

    fresh_ledger = AuditLedger(db_path)
    valid, error = fresh_ledger.verify()
    assert valid is False
    assert "tampered" in error


def test_reversal_within_window_succeeds_then_blocks_double_reversal(sentinel):
    intent = make_intent(idempotency_key="rev_1")
    resp = sentinel.submit(intent)
    result = sentinel.reverse(resp.execution.tx_id, reason="test", requested_by="tester")
    assert result["reversed"] is True
    second = sentinel.reverse(resp.execution.tx_id, reason="test again", requested_by="tester")
    assert second["reversed"] is False
    assert second["reason"] == "already reversed"


def test_human_escalation_resolution_can_execute_after_the_fact(sentinel):
    intent = make_intent(target_entity="denied_acct", amount=10.0)
    resp = sentinel.submit(intent)
    assert resp.execution is None
    resolved = sentinel.resolve_escalation(resp.escalation_id, approve=True, resolver="ops_test")
    assert resolved.verdict.value == "human_approved"
    assert resolved.execution is not None
