"""
Cerebras agent — fastest free Llama inference on the planet (~2000 tok/s).

OpenAI-compatible endpoint at https://api.cerebras.ai/v1. Free tier with
generous rate limits, no credit card required. Default model is
llama-3.3-70b; override with CEREBRAS_MODEL env var. Other free options:
llama3.1-8b, llama-4-scout-17b-16e-instruct, qwen-3-32b, gpt-oss-120b.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from app.models import AgentResponse

log = logging.getLogger("legion.agent.cerebras")


class CerebrasAgent:
    agent_id = "cerebras"

    def __init__(self) -> None:
        self.enabled = os.environ.get("CEREBRAS_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("CEREBRAS_API_KEY", "")
        self.model = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")
        self.base_url = os.environ.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="disabled",
            )
        if not self.api_key:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="no_api_key",
            )
        start = time.monotonic()
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "max_tokens": 1024,
            "temperature": 0.7,
        }
        try:
            async with httpx.AsyncClient(timeout=deadline_ms / 1000) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="timeout",
            )
        except httpx.HTTPStatusError as exc:
            log.warning("cerebras HTTP %s", exc.response.status_code)
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=f"http_{exc.response.status_code}",
            )
        except Exception as exc:
            log.warning("cerebras error: %s", type(exc).__name__)
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=type(exc).__name__,
            )
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError):
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="malformed_response",
            )
        if not text:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="empty_output",
            )
        return AgentResponse(
            agent_id=self.agent_id, content=text, success=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=0.65, cost_cents=0.0,  # free tier
        )
