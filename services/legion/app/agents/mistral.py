"""
Mistral La Plateforme agent — OpenAI-compatible /v1/chat/completions.

Why Mistral: free tier (no credit card, 1 RPS, ~500K tokens/min) with
Codestral as the headline model — currently one of the best free code
models available. Default points at codestral-latest which makes this
agent the hive's specialist for coding queries; override via
MISTRAL_MODEL for general-purpose tasks (mistral-large-latest,
mistral-small-latest, etc).

Free-tier model IDs (verified late 2025):
  codestral-latest            — code (default)
  codestral-2501              — pinned codestral release
  mistral-large-latest        — general flagship
  mistral-small-latest        — fast, cheaper
  open-mistral-nemo           — Apache-2.0 12B model
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from app.models import AgentResponse

log = logging.getLogger("legion.agent.mistral")


class MistralAgent:
    agent_id = "mistral"

    def __init__(self) -> None:
        self.enabled = os.environ.get("MISTRAL_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("MISTRAL_API_KEY", "")
        self.model = os.environ.get("MISTRAL_MODEL", "codestral-latest")
        self.base_url = os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")

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
            body_snippet = exc.response.text[:300].replace("\n", " ")
            log.warning(
                "mistral HTTP %s on model=%s body=%s",
                exc.response.status_code, self.model, body_snippet,
            )
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=f"http_{exc.response.status_code}",
            )
        except Exception as exc:
            log.warning("mistral error: %s", type(exc).__name__)
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
        # Codestral specifically scores higher than the general default
        # because we deliberately routed it to fill the code-specialist slot.
        confidence = 0.70 if self.model.startswith("codestral") else 0.60
        return AgentResponse(
            agent_id=self.agent_id, content=text, success=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=confidence, cost_cents=0.0,
        )
