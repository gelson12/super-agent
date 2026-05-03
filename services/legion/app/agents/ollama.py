"""
Ollama agent — local LLM inference via a running Ollama instance.

Set OLLAMA_HOST to the host:port (or full URL) of a running Ollama service.
If no Ollama instance is reachable on first call, the agent self-disables
for the lifetime of the container rather than burning the full hive deadline
on a ConnectError every round.
"""
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
        except httpx.ConnectError:
            # Ollama not running — permanently disable so future hive rounds don't
            # waste their deadline budget on a connection that will never succeed.
            self.enabled = False
            log.warning(
                "ollama: auto-disabled — host %s unreachable (ConnectError). "
                "Set OLLAMA_HOST to a running instance to re-enable.",
                self.base_url,
            )
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="connect_error_auto_disabled",
            )
        except httpx.TimeoutException:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="timeout",
            )
        except httpx.HTTPStatusError as exc:
            log.warning("ollama HTTP %s", exc.response.status_code)
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=f"http_{exc.response.status_code}",
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
