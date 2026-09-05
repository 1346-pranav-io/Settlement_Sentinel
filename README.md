# Settlement Sentinel

Trust middleware for agentic payment settlements. This is a working
prototype of the idea we discussed: a choke point that every autonomous
agent (refund-recovery, payout, dispute-response, etc.) must pass through
before it touches real settlement rails — with a deterministic risk engine,
an independent LLM judge for flagged cases, an immutable audit ledger, and
reversible/idempotent execution.

## Why this exists

If ten different merchant-side agents each talk directly to a settlement
API, ten teams each reinvent "how do I stop this agent from approving a
fraudulent refund." This centralizes that as a single, testable, auditable
layer. The core design rule everywhere in this codebase: **fail closed**.
Any error, timeout, or disagreement routes to a human — never to
auto-approval.

## Architecture

```
Agent (Refund/Payout/Dispute) 
        │  submits SettlementIntent
        ▼
Settlement Sentinel gateway
        │
        ▼
Risk rules engine (deterministic, <1ms)
        │
        ├─ hard block (denylist) ───────────────► Human escalation queue
        │
        ├─ low risk, clean ─────────────────────► Execute directly
        │
        └─ flagged / low confidence
                │
                ▼
        LLM judge (independent second opinion)
                │
                ├─ approve ──────────────────────► Execute
                ├─ reject ───────────────────────► Rejected, nothing executes
                └─ escalate / judge fails ───────► Human escalation queue

Every branch writes to the hash-chained audit ledger before returning.
Executed transactions can be reversed within their reversal window.
```

## Project layout

```
app/
  models.py       - SettlementIntent, RiskAssessment, JudgeVerdict, Decision, etc.
  risk_engine.py  - deterministic fast-path rules (denylist, amount ceilings, velocity)
  judge_agent.py  - independent LLM judge (real Claude call if ANTHROPIC_API_KEY is set,
                    else a clearly-labeled heuristic fallback so it still runs without a key)
  execution.py    - mock settlement API: idempotent execution + reversal window
  ledger.py       - hash-chained, tamper-evident audit ledger (SQLite-backed)
  escalation.py   - human review queue
  sentinel.py     - the orchestrator wiring all of the above together
  main.py         - FastAPI service exposing it over HTTP
agents/
  demo_agents.py  - simulated agents (RefundRecoveryAgent, PayoutAgent, DisputeResponseAgent)
                    standing in for real Agent Studio / Claude Agent SDK agents
demo.py           - runnable, no-server-needed walkthrough of every path
tests/
  test_sentinel.py - pytest coverage of the guarantees that actually matter
```

## Running it

Install dependencies:
```
pip install -r requirements.txt
```

**Option A — see the whole pipeline run, no server needed:**
```
python demo.py
```
This runs 8 scenarios end to end (auto-approve, judge-approve, judge-reject,
hard-block, idempotent retry, human resolution, reversal, and a tamper
attempt on the ledger that gets caught) and prints what happened at each
step.

**Option B — run it as a real service:**
```
uvicorn app.main:app --reload --port 8000
```
Then open `http://localhost:8000/docs` for interactive API docs, or:
```
curl -X POST http://localhost:8000/v1/settlement-intents \
  -H "Content-Type: application/json" \
  -d '{
    "action_type": "refund",
    "amount": 300,
    "merchant_id": "merch_1",
    "target_entity": "cust_1",
    "agent_id": "agent_refund_recovery_v1",
    "agent_reasoning": "Verified duplicate charge, refunding the duplicate.",
    "agent_confidence": 0.9,
    "idempotency_key": "unique-key-per-attempt"
  }'
```

**Run the tests:**
```
pytest tests/ -v
```

## Plugging in the real judge model

By default, with no `ANTHROPIC_API_KEY` set, the judge uses a heuristic
fallback (keyword scan for hedging language + amount/risk cross-check) so
the whole pipeline is runnable out of the box. To use a real Claude model as
the independent reviewer:
```
export ANTHROPIC_API_KEY=sk-ant-...
python demo.py     # will now print "Judge running in: LLM (Claude)"
```
`app/judge_agent.py` calls the Messages API directly with a system prompt
that frames the model as an independent auditor with no stake in the
outcome. Any failure in that call (timeout, malformed JSON, network error)
is caught and converted into an `escalate` verdict — it never silently
approves.

## What's deliberately out of scope for this prototype

- The actual refund/payout/dispute agents themselves — `agents/demo_agents.py`
  simulates what they'd send, standing in for real Claude Agent SDK agents.
- Multi-node consensus for the ledger — we're the sole writer, so a hash
  chain gives tamper-evidence without needing blockchain-style consensus.
- Auth/rate-limiting between agents and the sentinel — a real deployment
  would put this behind per-merchant API keys and scoped permissions.

## Extending it

- Add more risk rules in `risk_engine.py` (e.g. geo mismatches, new-account
  age checks).
- Swap the SQLite ledger for an append-only table in your real database, or
  add periodic anchoring of the chain's latest hash to an external system
  for extra tamper-evidence.
- Add a second, independent risk model as a "consensus" step alongside the
  LLM judge — require both to agree before auto-executing above a threshold.
