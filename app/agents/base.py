from __future__ import annotations

import asyncio
import time
from typing import Protocol

from app.models import AgentResponse


class Agent(Protocol):
    agent_id: str
    enabled: bool

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse: ...


async def run_with_deadline(agent: Agent, query: str, deadline_ms: int) -> AgentResponse:
    """Enforce deadline at the orchestrator level regardless of agent internals."""
    start = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            agent.respond(query, deadline_ms), timeout=deadline_ms / 1000
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
