"""
SambaNova Cloud agent — OpenAI-compatible /v1/chat/completions.

Why SambaNova: ~500-1000 tok/s inference on Llama-3.3-70B (faster than
Cerebras for the same family) on a free tier with no credit card.
Their hardware is custom RDU silicon, so latency profile is genuinely
different from GPU-backed providers — useful for early-termination
wins in the hive engine.

Verified free-tier model IDs (late 2025):
  Meta-Llama-3.3-70B-Instruct        (default)
  Meta-Llama-3.1-405B-Instruct       (huge, slower but very strong)
  Meta-Llama-3.1-70B-Instruct
  Meta-Llama-3.1-8B-Instruct         (lightweight)
  Llama-3.2-1B/3B-Instruct           (tiny, very fast)
  Llama-3.2-11B/90B-Vision-Instruct  (multimodal)
  Qwen2.5-72B-Instruct
  DeepSeek-R1-Distill-Llama-70B      (reasoning)

Override the default with SAMBANOVA_MODEL.

Resilience:
  1. 429 rate-limit tracking via quota_state (10-min cooldown)
  2. Up to 2 retries on transient 5xx with exponential backoff (0.5s, 1s)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from app import quota_state
from app.models import AgentResponse

log = logging.getLogger("legion.agent.sambanova")

_RATE_LIMIT_COOLDOWN_S = 600
_MAX_5XX_RETRIES       = 2
_RETRY_BACKOFF_S       = [0.5, 1.0]
_MIN_BUDGET_MS         = 1500


class SambaNovaAgent:
    agent_id = "sambanova"

    def __init__(self) -> None:
        self.enabled = os.environ.get("SAMBANOVA_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("SAMBANOVA_API_KEY", "")
        self.model = os.environ.get(
            "SAMBANOVA_MODEL", "Meta-Llama-3.3-70B-Instruct",
        )
        self.base_url = os.environ.get(
            "SAMBANOVA_BASE_URL", "https://api.sambanova.ai/v1",
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
        if quota_state.is_exhausted(self.agent_id, self.model):
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="rate_limit_cooldown",
            )

        t0 = time.monotonic()
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

        for attempt in range(1 + _MAX_5XX_RETRIES):
            remaining_ms = deadline_ms - int((time.monotonic() - t0) * 1000)
            if remaining_ms < _MIN_BUDGET_MS:
                break
            try:
                async with httpx.AsyncClient(timeout=remaining_ms / 1000) as client:
                    resp = await client.post(url, headers=headers, json=body)
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.TimeoutException:
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    self_confidence=0.0, error_class="timeout",
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                body_snippet = exc.response.text[:300].replace("\n", " ")
                log.warning("sambanova HTTP %s model=%s body=%s", status, self.model, body_snippet)
                latency_ms = int((time.monotonic() - t0) * 1000)
                if status == 429:
                    quota_state.mark_exhausted_for(
                        self.agent_id, self.model, _RATE_LIMIT_COOLDOWN_S, reason="upstream_429"
                    )
                    return AgentResponse(
                        agent_id=self.agent_id, content=None, success=False,
                        latency_ms=latency_ms, self_confidence=0.0, error_class="http_429",
                    )
                if 500 <= status < 600 and attempt < _MAX_5XX_RETRIES:
                    backoff = _RETRY_BACKOFF_S[attempt]
                    if remaining_ms > int(backoff * 1000) + _MIN_BUDGET_MS:
                        log.info("sambanova: transient %s — retry %d in %.1fs", status, attempt + 1, backoff)
                        await asyncio.sleep(backoff)
                        continue
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=latency_ms, self_confidence=0.0,
                    error_class=f"http_{status}",
                )
            except Exception as exc:
                log.warning("sambanova error: %s", type(exc).__name__)
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    self_confidence=0.0, error_class=type(exc).__name__,
                )

            try:
                text = (data["choices"][0]["message"]["content"] or "").strip()
            except (KeyError, IndexError):
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    self_confidence=0.0, error_class="malformed_response",
                )
            if not text:
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    self_confidence=0.0, error_class="empty_output",
                )
            return AgentResponse(
                agent_id=self.agent_id, content=text, success=True,
                latency_ms=int((time.monotonic() - t0) * 1000),
                self_confidence=0.65, cost_cents=0.0,
            )

        return AgentResponse(
            agent_id=self.agent_id, content=None, success=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            self_confidence=0.0, error_class="budget_exhausted",
        )
