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

A 2026-04-25 health probe (scripts/probe_openrouter_health.sh) confirms
the Llama family is currently throttled at the Venice upstream while
Google/OpenAI/NVIDIA-routed models serve normally. Default chain
prefers the verified-OK ones.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from app import quota_state
from app.models import AgentResponse

log = logging.getLogger("legion.agent.openrouter")


# Verified-healthy free models on the user's tier (2026-04-25 probe).
# When the preferred model 429s ('temporarily rate-limited upstream'),
# we mark it exhausted for ~10 min and try the next.
DEFAULT_MODEL_CHAIN = [
    "google/gemma-3-4b-it:free",
    "google/gemma-3-12b-it:free",
    "openai/gpt-oss-20b:free",
    "nvidia/nemotron-nano-9b-v2:free",
]
RATE_LIMIT_COOLDOWN_S = 600  # 10 min — most OpenRouter 429s clear in <5min


class OpenRouterAgent:
    agent_id = "openrouter"

    def __init__(self) -> None:
        self.enabled = os.environ.get("OPENROUTER_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        preferred = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL_CHAIN[0])
        self.model_chain: list[str] = (
            [preferred] + [m for m in DEFAULT_MODEL_CHAIN if m != preferred]
        )
        self.base_url = os.environ.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        )
        # Optional referer headers OpenRouter requests for analytics
        self.referer = os.environ.get("OPENROUTER_REFERER", "https://legion-production-36db.up.railway.app")
        self.title = os.environ.get("OPENROUTER_APP_TITLE", "Legion Engineer")

    async def _try_model(
        self, model: str, query: str, deadline_ms: int,
    ) -> tuple[AgentResponse, int | None, str]:
        """
        Returns (response, http_status_or_None, error_body). When the
        upstream returns HTTP 429 the caller will mark this model
        rate-limited and try the next in the chain.
        """
        start = time.monotonic()
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.referer,
            "X-Title": self.title,
        }
        body = {
            "model": model,
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
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    self_confidence=0.0, error_class="timeout",
                ),
                None, "",
            )
        except httpx.HTTPStatusError as exc:
            body_snippet = exc.response.text[:300].replace("\n", " ")
            log.warning(
                "openrouter HTTP %s on model=%s body=%s",
                exc.response.status_code, model, body_snippet,
            )
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    self_confidence=0.0,
                    error_class=f"http_{exc.response.status_code}",
                ),
                exc.response.status_code, exc.response.text,
            )
        except Exception as exc:
            log.warning("openrouter error: %s", type(exc).__name__)
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    self_confidence=0.0, error_class=type(exc).__name__,
                ),
                None, "",
            )
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError):
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    self_confidence=0.0, error_class="malformed_response",
                ),
                200, "",
            )
        if not text:
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    self_confidence=0.0, error_class="empty_output",
                ),
                200, "",
            )
        return (
            AgentResponse(
                agent_id=self.agent_id, content=text, success=True,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.55, cost_cents=0.0,
            ),
            200, "",
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
        per_model_deadline = max(int(deadline_ms / max(len(self.model_chain), 1)), 4000)
        last_response: AgentResponse | None = None
        for model in self.model_chain:
            if quota_state.is_exhausted(self.agent_id, model):
                log.info("openrouter: skipping %s (rate-limit cooldown)", model)
                continue
            response, status, _body = await self._try_model(
                model, query, per_model_deadline,
            )
            if response.success:
                if model != self.model_chain[0]:
                    log.info("openrouter: served via fallback model %s", model)
                return response
            last_response = response
            # 429 = upstream rate-limit on this model — mark it for short
            # cooldown and try the next model. Other failures bubble up.
            if status == 429:
                quota_state.mark_exhausted_for(
                    self.agent_id, model, RATE_LIMIT_COOLDOWN_S,
                    reason="upstream_429",
                )
                continue
            return response
        if last_response is not None:
            return last_response
        return AgentResponse(
            agent_id=self.agent_id, content=None, success=False,
            latency_ms=0, self_confidence=0.0,
            error_class="all_models_rate_limited",
        )
