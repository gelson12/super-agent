"""
DeepSeek agent — OpenAI-compatible /v1/chat/completions.

Why DeepSeek: deepseek-reasoner (R1) is an open-weight chain-of-thought
model that fills the hive's "reasoning" slot — math, planning, complex
multi-step problems where chat-tuned models often shortcut. Free tier
varies (sometimes credit-funded, sometimes daily quota); pay tier is
also extremely cheap so a small top-up sustains heavy use.

Models:
  deepseek-reasoner   — R1 reasoning (default, slower but strongest)
  deepseek-chat       — V3 chat (fast, general-purpose fallback)

Override with DEEPSEEK_MODEL. The R1 'reasoner' returns content as
usual but also includes 'reasoning_content' which we ignore for now
(we only want the final answer to score in the hive).
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from app.models import AgentResponse

log = logging.getLogger("legion.agent.deepseek")


class DeepSeekAgent:
    agent_id = "deepseek"

    def __init__(self) -> None:
        self.enabled = os.environ.get("DEEPSEEK_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-reasoner")
        self.base_url = os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1",
        )

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
        # R1 streams a 'reasoning_content' alongside 'content'; we only
        # care about the final answer, so cap max_tokens generously since
        # internal chain-of-thought eats output budget.
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "max_tokens": 2048,
            "temperature": 0.6,
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
                "deepseek HTTP %s on model=%s body=%s",
                exc.response.status_code, self.model, body_snippet,
            )
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=f"http_{exc.response.status_code}",
            )
        except Exception as exc:
            log.warning("deepseek error: %s", type(exc).__name__)
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
        # Reasoner gets a higher self-confidence on tasks where it shines;
        # chat-V3 stays at the general-purpose default.
        confidence = 0.72 if self.model == "deepseek-reasoner" else 0.62
        return AgentResponse(
            agent_id=self.agent_id, content=text, success=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=confidence, cost_cents=0.0,
        )
