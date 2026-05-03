from __future__ import annotations

import asyncio
import time
from typing import Protocol

from app.models import AgentResponse


class Agent(Protocol):
    agent_id: str
    enabled: bool

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse: ...


# Phrases that signal the agent is hedging or refusing — low confidence.
_LOW_CONFIDENCE_PHRASES = (
    "i don't know", "i'm not sure", "i cannot", "i can't help",
    "as an ai, i", "i'm an ai", "i apologize, but", "i'm unable to",
    "unfortunately, i", "i don't have access",
)


def _estimate_confidence(content: str) -> float:
    """
    Heuristic self-confidence from answer content.
    Agents rarely set self_confidence themselves; this fills the gap so the
    hive's early-termination (threshold=0.85) and winner selection can
    distinguish a thorough answer from a brief or hedging one.

    Scale:
      0.0–0.35  hedging / refusal phrases detected
      0.40–0.60 very short answer (< 80 chars) — might be incomplete
      0.60–0.80 moderate length, no red flags
      0.80–0.92 long, structured answer (>= 500 chars with punctuation)
    """
    if not content:
        return 0.0
    lower = content.lower()
    if any(p in lower for p in _LOW_CONFIDENCE_PHRASES):
        return 0.30
    length = len(content)
    if length < 80:
        return 0.45
    # Structural signals: sentences and paragraphs suggest completeness
    structure_bonus = 0.05 if (content.count(".") + content.count("\n")) > 3 else 0.0
    # Code blocks signal a concrete, actionable answer
    code_bonus = 0.05 if "```" in content or "    " in content else 0.0
    base = min(0.87, 0.55 + length / 2000)
    return min(0.92, base + structure_bonus + code_bonus)


async def run_with_deadline(agent: Agent, query: str, deadline_ms: int) -> AgentResponse:
    """Enforce deadline at the orchestrator level regardless of agent internals."""
    start = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            agent.respond(query, deadline_ms), timeout=deadline_ms / 1000
        )
        # Backfill self_confidence when the agent left it at the default (0.5).
        # This lets early termination and winner-selection distinguish answer quality.
        if resp.success and resp.content and resp.self_confidence == 0.5:
            resp = resp.model_copy(
                update={"self_confidence": _estimate_confidence(resp.content)}
            )
        return resp
    except asyncio.TimeoutError:
        return AgentResponse(
            agent_id=agent.agent_id,
            content=None,
            success=False,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=0.0,
            error_class="timeout",
        )
    except Exception as exc:
        return AgentResponse(
            agent_id=agent.agent_id,
            content=None,
            success=False,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=0.0,
            error_class=type(exc).__name__,
        )
