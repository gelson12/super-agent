"""
OpenRouter agent — OpenAI-compatible aggregator with many free models.

Free models on OpenRouter (suffix `:free` does not consume credits).
Verified 2026-04-25 via /v1/models — current free roster includes:
  meta-llama/llama-3.3-70b-instruct:free
  meta-llama/llama-3.2-3b-instruct:free
  google/gemma-3-27b-it:free
  google/gemma-3-12b-it:free
  openai/gpt-oss-120b:free
  openai/gpt-oss-20b:free
  qwen/qwen3-coder:free
  nousresearch/hermes-3-llama-3.1-405b:free
  ...and several more rotating in/out.

Override the default with OPENROUTER_MODEL env var. Run
scripts/probe_openrouter_models.sh inside the container to refresh.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from app.models import AgentResponse

log = logging.getLogger("legion.agent.openrouter")


class OpenRouterAgent:
    agent_id = "openrouter"

    def __init__(self) -> None:
        self.enabled = os.environ.get("OPENROUTER_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.model = os.environ.get(
            "OPENROUTER_MODEL", "meta-llama/llama-3.2-3b-instruct:free"
        )
        self.base_url = os.environ.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        )
        # Optional referer headers OpenRouter requests for analytics
        self.referer = os.environ.get("OPENROUTER_REFERER", "https://legion-production-36db.up.railway.app")
        self.title = os.environ.get("OPENROUTER_APP_TITLE", "Legion Engineer")

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
            "HTTP-Referer": self.referer,
            "X-Title": self.title,
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
                "openrouter HTTP %s on model=%s body=%s",
                exc.response.status_code, self.model, body_snippet,
            )
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=f"http_{exc.response.status_code}",
            )
        except Exception as exc:
            log.warning("openrouter error: %s", type(exc).__name__)
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
            self_confidence=0.55, cost_cents=0.0,
        )
