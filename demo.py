"""
Run this directly: `python demo.py`

Walks through every path a settlement intent can take through the sentinel,
using the simulated agents in agents/demo_agents.py. No server needed - this
imports the sentinel directly. Uses a fresh in-memory-ish sqlite ledger file
each run (data/demo_ledger.db) so you can inspect it afterwards.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.escalation import EscalationQueue
from app.execution import SettlementExecutor
from app.judge_agent import JudgeAgent
from app.ledger import AuditLedger
from app.risk_engine import RiskRulesEngine
from app.sentinel import SettlementSentinel
from agents.demo_agents import DisputeResponseAgent, PayoutAgent, RefundRecoveryAgent

DB_PATH = "data/demo_ledger.db"


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show(response, label: str) -> None:
    print(f"\n[{label}]")
    print(f"  verdict      : {response.verdict.value}")
    print(f"  risk_score   : {response.risk_score}")
    print(f"  flags        : {response.flags}")
    if response.judge_verdict:
        print(f"  judge        : {response.judge_verdict.decision} "
              f"(source={response.judge_verdict.source}) - {response.judge_verdict.reasoning}")
    if response.execution:
        print(f"  execution    : tx_id={response.execution.tx_id} "
              f"replay={response.execution.idempotent_replay}")
    if response.escalation_id:
        print(f"  escalation_id: {response.escalation_id}")
    print(f"  message      : {response.message}")


def main() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    sentinel = SettlementSentinel(
        risk_engine=RiskRulesEngine(denylist={"acct_flagged_9911"}),
        judge=JudgeAgent(),  # picks up ANTHROPIC_API_KEY if set, else heuristic fallback
        executor=SettlementExecutor(),
        ledger=AuditLedger(DB_PATH),
        escalation_queue=EscalationQueue(),
    )
    judge_mode = "LLM (Claude)" if sentinel.judge.api_key else "heuristic fallback (no ANTHROPIC_API_KEY set)"
    print(f"Judge running in: {judge_mode}")

    refund_agent = RefundRecoveryAgent()
    payout_agent = PayoutAgent()
    dispute_agent = DisputeResponseAgent()

    # --- Scenario 1: low-risk refund, clean reasoning -> auto-approved ---
    banner("Scenario 1: small refund, high agent confidence -> should auto-approve")
    intent1 = refund_agent.routine_refund("merch_101", "cust_5001", amount=450.0)
    r1 = sentinel.submit(intent1)
    show(r1, "routine refund")

    # --- Scenario 2: large payout -> above ceiling -> judge reviews, approves ---
    banner("Scenario 2: large but well-justified payout -> judge should approve")
    intent2 = payout_agent.large_payout("merch_202", "bank_acct_7788", amount=45_000.0)
    r2 = sentinel.submit(intent2)
    show(r2, "large payout")

    # --- Scenario 3: agent itself is unsure -> judge should reject ---
    banner("Scenario 3: agent hedges in its own reasoning -> judge should reject")
    intent3 = refund_agent.uncertain_refund("merch_101", "cust_5002", amount=1_200.0)
    r3 = sentinel.submit(intent3)
    show(r3, "uncertain refund")

    # --- Scenario 4: denylisted target -> hard block, straight to human ---
    banner("Scenario 4: payout to a denylisted account -> hard block")
    intent4 = payout_agent.payout_to_denylisted_account("merch_303", "acct_flagged_9911", amount=8_000.0)
    r4 = sentinel.submit(intent4)
    show(r4, "denylisted payout")

    # --- Scenario 5: dispute response -> always reviewed by judge ---
    banner("Scenario 5: dispute response with solid evidence -> judge reviews")
    intent5 = dispute_agent.submit_evidence("merch_202", "dispute_88213", amount=3_500.0)
    r5 = sentinel.submit(intent5)
    show(r5, "dispute response")

    # --- Scenario 6: idempotent retry of scenario 1 ---
    banner("Scenario 6: agent retries scenario 1 with the SAME idempotency key")
    r6 = sentinel.submit(intent1)  # identical intent object, same idempotency_key
    show(r6, "retry of routine refund")
    assert r6.execution.idempotent_replay is True, "expected an idempotent replay, not a fresh execution"
    assert r6.execution.tx_id == r1.execution.tx_id, "idempotency key reused a different tx_id - BUG"
    print("  -> idempotency verified: same tx_id returned, no double execution")

    # --- Scenario 7: resolve the human escalation from scenario 4 ---
    banner("Scenario 7: a human ops reviewer resolves the escalated denylist case")
    r7 = sentinel.resolve_escalation(r4.escalation_id, approve=False, resolver="ops_priya", note="Confirmed fraud ring account, keeping blocked.")
    show(r7, "human resolution")

    # --- Scenario 8: reverse a settled transaction ---
    banner("Scenario 8: reverse the auto-approved refund from scenario 1")
    reversal = sentinel.reverse(r1.execution.tx_id, reason="Customer disputed the refund amount after the fact", requested_by="ops_priya")
    print(f"\n[reversal]\n  {reversal}")

    # --- Ledger integrity check ---
    banner("Audit ledger")
    entries = sentinel.ledger.all_entries()
    print(f"Total ledger entries: {len(entries)}")
    valid, error = sentinel.ledger.verify()
    print(f"Chain valid: {valid}" + (f" ({error})" if error else ""))

    banner("Tamper test: mutating a historical ledger row directly in the DB")
    import json
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT payload FROM ledger WHERE seq = 1").fetchone()
    tampered_payload = json.loads(row[0])
    # Someone with raw DB access tries to quietly bump a refund amount after the fact.
    if tampered_payload.get("intent"):
        tampered_payload["intent"]["amount"] = 999999.0
    conn.execute(
        "UPDATE ledger SET payload = ? WHERE seq = 1",
        (json.dumps(tampered_payload, sort_keys=True, separators=(",", ":")),),
    )
    conn.commit()
    conn.close()
    tampered_sentinel_ledger = AuditLedger(DB_PATH)
    valid_after, error_after = tampered_sentinel_ledger.verify()
    print(f"Chain valid after tamper attempt: {valid_after} (expected False)")
    print(f"Detected at: {error_after}")

    banner("Done")
    print(f"Full ledger persisted at {DB_PATH} - inspect with sqlite3 or the /v1/ledger API.")


if __name__ == "__main__":
    main()
