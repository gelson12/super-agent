from __future__ import annotations

import logging
import os
import time

import httpx

from app.models import AgentResponse

log = logging.getLogger("legion.agent.ollama")


class OllamaAgent:
    agent_id = "ollama"

    def __init__(self) -> None:
        self.enabled = os.environ.get("OLLAMA_ENABLED", "false").lower() == "true"
        host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
        # accept bare host:port or full URL
        self.base_url = host if host.startswith("http") else f"http://{host}"
        self.model = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="disabled",
            )
        start = time.monotonic()
        url = f"{self.base_url.rstrip('/')}/api/generate"
        try:
            async with httpx.AsyncClient(timeout=deadline_ms / 1000) as client:
                resp = await client.post(url, json={
                    "model": self.model,
                    "prompt": query,
                    "stream": False,
                })
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="timeout",
            )
        except Exception as exc:
            log.warning("ollama error: %s", type(exc).__name__)
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=type(exc).__name__,
            )
        text = (data.get("response") or "").strip()
        if not text:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="empty_output",
            )
        return AgentResponse(
            agent_id=self.agent_id, content=text, success=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=0.45,
        )
