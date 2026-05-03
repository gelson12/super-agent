"""
GitHub Models agent — free GPT-4o-mini, Llama-3.3-70B, Phi, Mistral via
GitHub Personal Access Token (scope `models:read`). Endpoint is OpenAI-
compatible: https://models.github.ai/inference/chat/completions.

Models are namespaced (e.g. `openai/gpt-4o-mini`, `meta/Llama-3.3-70B-Instruct`).
Default: openai/gpt-4o-mini. Override with GITHUB_MODELS_MODEL env var.

GITHUB_MODELS_TOKEN should be a fine-grained PAT with the `models:read`
permission. A regular `repo`-scope PAT also works for Models access.

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

log = logging.getLogger("legion.agent.github_models")

_RATE_LIMIT_COOLDOWN_S = 600
_MAX_5XX_RETRIES       = 2
_RETRY_BACKOFF_S       = [0.5, 1.0]
_MIN_BUDGET_MS         = 1500


class GitHubModelsAgent:
    agent_id = "github_models"

    def __init__(self) -> None:
        self.enabled = os.environ.get("GITHUB_MODELS_ENABLED", "false").lower() == "true"
        # Accept either GITHUB_MODELS_TOKEN or fall back to existing GITHUB_PAT.
        self.api_key = (
            os.environ.get("GITHUB_MODELS_TOKEN")
            or os.environ.get("GITHUB_PAT")
            or ""
        )
        self.model = os.environ.get("GITHUB_MODELS_MODEL", "openai/gpt-4o-mini")
        self.base_url = os.environ.get(
            "GITHUB_MODELS_BASE_URL", "https://models.github.ai/inference"
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
            "Accept": "application/vnd.github+json",
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
                log.warning("github_models HTTP %s model=%s", status, self.model)
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
                        log.info("github_models: transient %s — retry %d in %.1fs", status, attempt + 1, backoff)
                        await asyncio.sleep(backoff)
                        continue
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=latency_ms, self_confidence=0.0,
                    error_class=f"http_{status}",
                )
            except Exception as exc:
                log.warning("github_models error: %s", type(exc).__name__)
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
                self_confidence=0.6, cost_cents=0.0,
            )

        return AgentResponse(
            agent_id=self.agent_id, content=None, success=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            self_confidence=0.0, error_class="budget_exhausted",
        )
