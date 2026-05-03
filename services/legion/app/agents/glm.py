"""
GLM agent — ZhipuAI GLM-4.7 via Cerebras (no separate key needed).

GLM-4.7 (zai-glm-4.7) is available directly on Cerebras Cloud's free tier,
so this agent reuses the existing CEREBRAS_API_KEY — no ZhipuAI account needed.

If a direct ZhipuAI key (GLM_API_KEY) is also configured, that is tried first
as it gives access to the newer glm-4-flash and glm-z1-flash reasoning model.
Otherwise falls straight through to the Cerebras path.

Resilience design:
  1. Dual-path routing: direct ZhipuAI API → Cerebras (automatic, key-driven)
  2. Per-model 429 rate-limit tracking via quota_state (10-min cooldown)
  3. Up to 2 retries on transient 5xx with exponential backoff (0.5s, 1s)
  4. Deadline budget split equally across paths
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

# ── Path 1: direct ZhipuAI API (only if GLM_API_KEY set) ─────────────────────
_ZHIPU_BASE   = "https://open.bigmodel.cn/api/paas/v4"
_ZHIPU_MODELS = ["glm-4-flash", "glm-z1-flash"]   # free tier models

# ── Path 2: Cerebras (GLM-4.7 available on free tier, uses CEREBRAS_API_KEY) ─
_CEREBRAS_BASE  = "https://api.cerebras.ai/v1"
_CEREBRAS_MODEL = "zai-glm-4.7"

_RATE_LIMIT_COOLDOWN_S = 600   # 10 min
_MAX_5XX_RETRIES       = 2
_RETRY_BACKOFF_S       = [0.5, 1.0]
_MIN_BUDGET_MS         = 1500


class GLMAgent:
    agent_id = "glm"

    def __init__(self) -> None:
        self.glm_key      = os.environ.get("GLM_API_KEY", "")
        self.cerebras_key = os.environ.get("CEREBRAS_API_KEY", "")
        self.enabled      = (
            os.environ.get("GLM_ENABLED", "false").lower() == "true"
            and bool(self.glm_key or self.cerebras_key)
        )

    # ── shared low-level HTTP call ────────────────────────────────────────────

    async def _call_once(
        self,
        base_url: str,
        api_key: str,
        model: str,
        query: str,
        timeout_s: float,
    ) -> tuple[str | None, int | None, str]:
        url = f"{base_url.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": query}],
                          "max_tokens": 1024,
                          "temperature": 0.7},
                )
                resp.raise_for_status()
                data = resp.json()
                text = (data["choices"][0]["message"]["content"] or "").strip()
                return (text or None), 200, ""
        except httpx.TimeoutException:
            return None, None, "timeout"
        except httpx.HTTPStatusError as exc:
            snippet = exc.response.text[:200].replace("\n", " ")
            log.warning("glm HTTP %s model=%s: %s", exc.response.status_code, model, snippet)
            return None, exc.response.status_code, exc.response.text
        except (KeyError, IndexError):
            return None, 200, "malformed_response"
        except Exception as exc:
            log.warning("glm error: %s", type(exc).__name__)
            return None, None, type(exc).__name__

    # ── retry wrapper ─────────────────────────────────────────────────────────

    async def _try_path(
        self,
        path_id: str,
        base_url: str,
        api_key: str,
        model: str,
        query: str,
        budget_ms: int,
    ) -> tuple[AgentResponse, int | None]:
        t0 = time.monotonic()
        for attempt in range(1 + _MAX_5XX_RETRIES):
            remaining_ms = budget_ms - int((time.monotonic() - t0) * 1000)
            if remaining_ms < _MIN_BUDGET_MS:
                break
            content, status, err = await self._call_once(
                base_url, api_key, model, query, remaining_ms / 1000
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if content:
                return AgentResponse(
                    agent_id=self.agent_id, content=content, success=True,
                    latency_ms=latency_ms, self_confidence=0.65, cost_cents=0.0,
                ), status
            if status == 429:
                return AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=latency_ms, self_confidence=0.0, error_class="http_429",
                ), 429
            if status is not None and 500 <= status < 600 and attempt < _MAX_5XX_RETRIES:
                backoff = _RETRY_BACKOFF_S[attempt]
                if remaining_ms > int(backoff * 1000) + _MIN_BUDGET_MS:
                    log.info("glm: transient %s — retry %d in %.1fs", status, attempt + 1, backoff)
                    await asyncio.sleep(backoff)
                    continue
            ec = f"http_{status}" if status else (err or "unknown_error")
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=latency_ms, self_confidence=0.0, error_class=ec,
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

        # Build ordered path list depending on which keys are available
        paths: list[tuple[str, str, str, str]] = []  # (path_id, base, key, model)
        if self.glm_key:
            for m in _ZHIPU_MODELS:
                paths.append((f"zhipu/{m}", _ZHIPU_BASE, self.glm_key, m))
        if self.cerebras_key:
            paths.append(("cerebras/glm-4.7", _CEREBRAS_BASE, self.cerebras_key, _CEREBRAS_MODEL))

        if not paths:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="no_api_key",
            )

        per_path_budget = max(int(deadline_ms / max(len(paths), 1)), 5000)
        last_response: AgentResponse | None = None

        for path_id, base_url, api_key, model in paths:
            if quota_state.is_exhausted(self.agent_id, path_id):
                log.info("glm: skipping %s (rate-limit cooldown)", path_id)
                continue
            response, status = await self._try_path(
                path_id, base_url, api_key, model, query, per_path_budget
            )
            if response.success:
                if path_id != paths[0][0]:
                    log.info("glm: served via fallback path %s", path_id)
                return response
            last_response = response
            if status == 429:
                quota_state.mark_exhausted_for(
                    self.agent_id, path_id, _RATE_LIMIT_COOLDOWN_S, reason="upstream_429"
                )
            # Always continue to next path regardless of error type

        if last_response is not None:
            return last_response
        return AgentResponse(
            agent_id=self.agent_id, content=None, success=False,
            latency_ms=0, self_confidence=0.0, error_class="all_paths_exhausted",
        )
