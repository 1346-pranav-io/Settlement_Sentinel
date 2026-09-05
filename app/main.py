"""
Settlement Sentinel API.

Run with:  uvicorn app.main:app --reload --port 8000
Docs at:   http://localhost:8000/docs
"""
from __future__ import annotations

import os
import sqlite3
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    # Absolute imports (used by Vercel serverless via api/index.py)
    from app.models import SentinelResponse, SettlementIntent
    from app.risk_engine import RiskRulesEngine
    from app.sentinel import SettlementSentinel
except ImportError:
    # Relative imports (used when running locally as a package: uvicorn app.main:app)
    from .models import SentinelResponse, SettlementIntent  # type: ignore
    from .risk_engine import RiskRulesEngine  # type: ignore
    from .sentinel import SettlementSentinel  # type: ignore

# In production this denylist would be loaded from a fraud/compliance service.
# Hardcoded here just so the demo has something concrete to hard-block on.
DEMO_DENYLIST = {"denied_acct_x", "acct_flagged_9911"}

app = FastAPI(
    title="Settlement Sentinel",
    description="Trust middleware for agentic payment settlements: every "
    "agent-initiated refund, payout, dispute response, or ledger adjustment "
    "passes through here before it touches real settlement rails.",
    version="0.1.0",
)

sentinel = SettlementSentinel(risk_engine=RiskRulesEngine(denylist=DEMO_DENYLIST))

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=FileResponse)
def root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Dashboard UI not found")


class ResolveEscalationRequest(BaseModel):
    approve: bool
    resolver: str
    note: str = ""


class ReversalRequest(BaseModel):
    tx_id: str
    reason: str
    requested_by: str


@app.post("/v1/settlement-intents", response_model=SentinelResponse)
def submit_intent(intent: SettlementIntent):
    """An agent submits a settlement action here. Never call the executor directly."""
    return sentinel.submit(intent)


@app.get("/v1/escalations")
def list_escalations():
    return [
        {
            "escalation_id": i.escalation_id,
            "status": i.status,
            "intent": i.intent.model_dump(mode="json"),
            "risk_score": i.risk_assessment.risk_score,
            "flags": i.risk_assessment.flags,
            "judge_verdict": i.judge_verdict.model_dump(mode="json") if i.judge_verdict else None,
            "created_at": i.created_at,
        }
        for i in sentinel.escalation_queue.pending()
    ]


@app.post("/v1/escalations/{escalation_id}/resolve", response_model=SentinelResponse)
def resolve_escalation(escalation_id: str, body: ResolveEscalationRequest):
    if sentinel.escalation_queue.get(escalation_id) is None:
        raise HTTPException(status_code=404, detail="escalation not found")
    return sentinel.resolve_escalation(escalation_id, body.approve, body.resolver, body.note)


@app.post("/v1/reversals")
def reverse_transaction(body: ReversalRequest):
    return sentinel.reverse(body.tx_id, body.reason, body.requested_by)


@app.get("/v1/ledger")
def get_ledger(limit: int = 100):
    entries = sentinel.ledger.all_entries()[-limit:]
    return [
        {"seq": e.seq, "timestamp": e.timestamp, "payload": e.payload, "hash": e.hash, "prev_hash": e.prev_hash}
        for e in entries
    ]


@app.get("/v1/ledger/verify")
def verify_ledger():
    valid, error = sentinel.ledger.verify()
    return {"valid": valid, "error": error}


@app.post("/v1/ledger/tamper-test")
def tamper_ledger_test():
    if sentinel.ledger.db_path and sentinel.ledger.db_path != ":memory:":
        conn = sqlite3.connect(sentinel.ledger.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE ledger SET payload_json = '{\"tampered\": true}' WHERE seq = 1")
        conn.commit()
        conn.close()
        return {"message": "Tampered row #1 directly in SQLite database."}
    # In memory fallback
    if len(sentinel.ledger._entries) > 1:
        sentinel.ledger._entries[1].payload = {"tampered": True}
        return {"message": "Tampered in-memory ledger entry #1."}
    return {"message": "Ledger has insufficient entries to tamper. Submit an intent first."}


@app.get("/v1/health")
def health():
    return {"status": "ok", "judge_mode": "llm" if sentinel.judge.api_key else "heuristic-fallback"}

