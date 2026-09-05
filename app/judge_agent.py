"""
Judge agent: an independent second opinion on flagged settlement intents.

Design principle that matters most here: FAIL CLOSED. If the LLM call
errors, times out, or returns something we can't parse, we do NOT default to
approve. We return "escalate" so a human sees it. A judge that quietly
fails open is worse than no judge at all.

If ANTHROPIC_API_KEY is set in the environment, this calls the real Claude
API as an independent reviewer. If not, it falls back to a heuristic judge
that is *deliberately* built from different signals than the risk engine
(keyword scan of the reasoning trace + relative amount check) so the demo
still exercises the full approve/reject/escalate pipeline without a key.
"""
from __future__ import annotations

import json
import os
import re

import requests

try:
    from app.models import JudgeVerdict, RiskAssessment, SettlementIntent
except ImportError:
    from .models import JudgeVerdict, RiskAssessment, SettlementIntent  # type: ignore

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
JUDGE_MODEL = "claude-sonnet-5"
REQUEST_TIMEOUT_SECONDS = 15

SYSTEM_PROMPT = """You are an independent financial-controls auditor reviewing a \
settlement action that an autonomous AI agent wants to execute (a refund, payout, \
dispute response, or ledger adjustment). You did not make this decision and you \
have no stake in it being approved - your only job is to catch mistakes and fraud.

You will be given the agent's intent, its own stated reasoning, and risk flags \
raised by a separate deterministic rules engine. Decide: approve, reject, or escalate.

- approve: the reasoning is sound, the amount and flags are unremarkable for this \
  kind of action, you would be comfortable if this executed with no human review.
- reject: the reasoning is clearly insufficient, inconsistent with the flags, or \
  suggests an error - this should NOT execute.
- escalate: you are not confident either way - a human should look at it.

Respond with ONLY a JSON object, no other text, no markdown fences:
{"decision": "approve|reject|escalate", "reasoning": "<one or two sentences>", \
"judge_confidence": <0.0-1.0>}
"""

_SUSPICIOUS_PATTERNS = re.compile(
    r"\b(guess|not sure|unverified|test transaction|placeholder|todo|unclear|"
    r"assume|might be|probably|random)\b",
    re.IGNORECASE,
)


class JudgeAgent:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def review(self, intent: SettlementIntent, risk: RiskAssessment) -> JudgeVerdict:
        if self.api_key:
            try:
                return self._review_with_llm(intent, risk)
            except Exception as exc:  # noqa: BLE001 - fail closed on ANY error
                return JudgeVerdict(
                    decision="escalate",
                    reasoning=f"Judge call failed ({exc.__class__.__name__}); "
                    "failing closed to human review rather than guessing.",
                    judge_confidence=0.0,
                    source="llm-error-failclosed",
                )
        return self._review_with_heuristics(intent, risk)

    # -- Real LLM path ---------------------------------------------------

    def _review_with_llm(self, intent: SettlementIntent, risk: RiskAssessment) -> JudgeVerdict:
        user_content = (
            f"Action type: {intent.action_type.value}\n"
            f"Amount: {intent.amount} {intent.currency}\n"
            f"Merchant: {intent.merchant_id}\n"
            f"Target entity: {intent.target_entity}\n"
            f"Submitting agent: {intent.agent_id}\n"
            f"Agent's stated confidence: {intent.agent_confidence}\n"
            f"Agent's reasoning trace: \"{intent.agent_reasoning}\"\n"
            f"Risk engine flags: {risk.flags}\n"
            f"Risk engine score: {risk.risk_score}/100\n"
        )
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": JUDGE_MODEL,
                "max_tokens": 300,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text)
        decision = parsed["decision"]
        if decision not in ("approve", "reject", "escalate"):
            raise ValueError(f"unexpected decision value: {decision}")
        return JudgeVerdict(
            decision=decision,
            reasoning=parsed.get("reasoning", ""),
            judge_confidence=float(parsed.get("judge_confidence", 0.5)),
            source="llm",
        )

    # -- Heuristic fallback (no API key configured) -----------------------

    def _review_with_heuristics(self, intent: SettlementIntent, risk: RiskAssessment) -> JudgeVerdict:
        """
        A deliberately different signal set from the risk engine, so the demo
        still shows real approve/reject/escalate branching without a key.
        This is NOT a substitute for the LLM judge in production - it's a
        clearly-labeled stand-in so the pipeline is runnable out of the box.
        """
        suspicious_hits = _SUSPICIOUS_PATTERNS.findall(intent.agent_reasoning)
        reasoning_len = len(intent.agent_reasoning.strip())

        if suspicious_hits:
            return JudgeVerdict(
                decision="reject",
                reasoning=(
                    f"Reasoning trace contains hedging/uncertain language "
                    f"({', '.join(sorted(set(m.lower() for m in suspicious_hits)))}); "
                    "not confident enough to let this execute unreviewed."
                ),
                judge_confidence=0.8,
                source="heuristic-fallback",
            )

        if reasoning_len < 15:
            return JudgeVerdict(
                decision="escalate",
                reasoning="Agent's reasoning trace is too thin to independently verify.",
                judge_confidence=0.3,
                source="heuristic-fallback",
            )

        if risk.risk_score >= 60:
            return JudgeVerdict(
                decision="escalate",
                reasoning=f"Risk score {risk.risk_score} is high enough that a human should confirm.",
                judge_confidence=0.5,
                source="heuristic-fallback",
            )

        return JudgeVerdict(
            decision="approve",
            reasoning="Reasoning trace is coherent, no suspicious language, risk score moderate.",
            judge_confidence=0.7,
            source="heuristic-fallback",
        )
