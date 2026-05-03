"""
GLM agent — ZhipuAI GLM-4-Flash / GLM-Z1-Flash (free tier).

ZhipuAI offers permanently free models on their BigModel platform:
  - glm-4-flash   — fast, versatile, multilingual (best for general tasks)
  - glm-z1-flash  — free reasoning model (like DeepSeek-R1-Distill, fires CoT)

Both are OpenAI-compatible at https://open.bigmodel.cn/api/paas/v4/.
Get a free API key at: https://open.bigmodel.cn/

Resilience design:
  1. Two-model fallback chain — glm-4-flash → glm-z1-flash.
     If primary is 429-rate-limited, switches to secondary automatically.
  2. Per-model rate-limit tracking via quota_state (10-min cooldown window).
  3. Up to 2 retries on transient 5xx errors with exponential backoff (0.5s, 1s),
     consuming from the per-model deadline budget before giving up.
  4. Deadline budget split equally across models so the total latency is bounded.
  5. Continue to next model on any non-429 failure too — maximises success rate.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from app import quota_state
from app.models import AgentResponse

log = logging.getLogger("legion.agent.glm")

# Free-tier model chain — tried in order on 429 or model exhaustion.
DEFAULT_MODEL_CHAIN = [
    "glm-4-flash",    # primary: best free general-purpose model
    "glm-z1-flash",   # fallback: free reasoning model (slower, better for complex tasks)
]

RATE_LIMIT_COOLDOWN_S = 600   # 10 min — ZhipuAI 429s typically clear in < 5 min
_MAX_5XX_RETRIES     = 2      # retry transient server errors up to twice per model
_RETRY_BACKOFF_S     = [0.5, 1.0]   # exponential delay between retries (seconds)
_MIN_BUDGET_MS       = 1500   # skip further attempts if less than this ms remains


class GLMAgent:
    agent_id = "glm"

    def __init__(self) -> None:
        self.enabled  = os.environ.get("GLM_ENABLED", "false").lower() == "true"
        self.api_key  = os.environ.get("GLM_API_KEY", "")
        preferred     = os.environ.get("GLM_MODEL", DEFAULT_MODEL_CHAIN[0])
        self.model_chain: list[str] = (
            [preferred] + [m for m in DEFAULT_MODEL_CHAIN if m != preferred]
        )
        self.base_url = os.environ.get(
            "GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
        )

    # ── low-level HTTP ────────────────────────────────────────────────────────

    async def _call_once(
        self, model: str, query: str, timeout_s: float,
    ) -> tuple[str | None, int | None, str]:
        """
        Single HTTP round-trip.
        Returns (content_or_None, http_status_or_None, error_tag).
        """
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": query}],
            "max_tokens": 1024,
            "temperature": 0.7,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = (data["choices"][0]["message"]["content"] or "").strip()
                return (text or None), 200, ""
        except httpx.TimeoutException:
            return None, None, "timeout"
        except httpx.HTTPStatusError as exc:
            snippet = exc.response.text[:200].replace("\n", " ")
            log.warning(
                "glm HTTP %s model=%s: %s",
                exc.response.status_code, model, snippet,
            )
            return None, exc.response.status_code, exc.response.text
        except (KeyError, IndexError):
            return None, 200, "malformed_response"
        except Exception as exc:
            log.warning("glm unexpected error: %s", type(exc).__name__)
            return None, None, type(exc).__name__

    # ── per-model attempt with 5xx retry ─────────────────────────────────────

    async def _try_model(
        self, model: str, query: str, budget_ms: int,
    ) -> tuple[AgentResponse, int | None]:
        """
        Attempt `model` with up to _MAX_5XX_RETRIES retries on transient 5xx.
        Returns (AgentResponse, final_http_status_or_None).
        The 429 status is returned as-is so the caller can rotate to next model.
        """
        t0 = time.monotonic()

        for attempt in range(1 + _MAX_5XX_RETRIES):
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            remaining_ms = budget_ms - elapsed_ms
            if remaining_ms < _MIN_BUDGET_MS:
                break

            timeout_s = remaining_ms / 1000
            content, status, err = await self._call_once(model, query, timeout_s)

            latency_ms = int((time.monotonic() - t0) * 1000)

            if content:
                return AgentResponse(
                    agent_id=self.agent_id, content=content, success=True,
                    latency_ms=latency_ms, self_confidence=0.65, cost_cents=0.0,
                ), status

            # 429 — caller handles quota rotation; don't waste retries here
            if status == 429:
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=latency_ms, self_confidence=0.0,
                    error_class="http_429",
                ), 429

            # Transient 5xx — retry with exponential backoff if budget allows
            if status is not None and 500 <= status < 600 and attempt < _MAX_5XX_RETRIES:
                backoff = _RETRY_BACKOFF_S[attempt]
                remaining_after_backoff = remaining_ms - latency_ms - int(backoff * 1000)
                if remaining_after_backoff > _MIN_BUDGET_MS:
                    log.info(
                        "glm: transient %s on %s — retry %d in %.1fs",
                        status, model, attempt + 1, backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

            # Non-retryable or budget gone
            err_class = f"http_{status}" if status else (err or "unknown_error")
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=latency_ms, self_confidence=0.0, error_class=err_class,
            ), status

        return AgentResponse(
            agent_id=self.agent_id, content=None, success=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            self_confidence=0.0, error_class="budget_exhausted",
        ), None

    # ── public interface ──────────────────────────────────────────────────────

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

        # Split total deadline equally across models so overall latency is bounded
        per_model_budget = max(
            int(deadline_ms / max(len(self.model_chain), 1)), 5000
        )
        last_response: AgentResponse | None = None

        for model in self.model_chain:
            if quota_state.is_exhausted(self.agent_id, model):
                log.info("glm: skipping %s (rate-limit cooldown active)", model)
                continue

            response, status = await self._try_model(model, query, per_model_budget)

            if response.success:
                if model != self.model_chain[0]:
                    log.info("glm: served via fallback model %s", model)
                return response

            last_response = response

            if status == 429:
                quota_state.mark_exhausted_for(
                    self.agent_id, model, RATE_LIMIT_COOLDOWN_S,
                    reason="upstream_429",
                )
                # Continue — try next model in chain
                continue

            # Non-429 failure: still try next model (maximise success rate)
            continue

        if last_response is not None:
            return last_response
        return AgentResponse(
            agent_id=self.agent_id, content=None, success=False,
            latency_ms=0, self_confidence=0.0,
            error_class="all_models_exhausted",
        )
