from __future__ import annotations

import asyncio
import logging
import os
import time

from app import quota_state
from app.models import AgentResponse

log = logging.getLogger("legion.agent.gemini_b")


# Free-tier daily-quota chain. First model is the preferred one;
# subsequent entries are fallbacks when the upstream returns
# TerminalQuotaError. Each model's exhaustion is persisted until next
# UTC midnight (which is when Google resets daily counters).
DEFAULT_MODEL_CHAIN = [
    "gemini-2.5-flash",       # 250/day, 10 RPM (recommended starting point)
    "gemini-2.0-flash",       # 1500/day, 15 RPM (older but very generous)
    "gemini-2.5-flash-lite",  # 1000/day, 15 RPM (tiny model, last-resort)
]


def _is_quota_error(stderr_text: str) -> bool:
    """Match Google's free-tier daily-quota signal in stderr."""
    if not stderr_text:
        return False
    return (
        "TerminalQuotaError" in stderr_text
        or "exhausted your daily quota" in stderr_text
        or "RESOURCE_EXHAUSTED" in stderr_text
    )


class GeminiBAgent:
    agent_id = "gemini_b"

    def __init__(self) -> None:
        self.enabled = os.environ.get("GEMINI_B_ENABLED", "false").lower() == "true"
        self.binary = os.environ.get("GEMINI_BINARY", "gemini")
        # Account B's API key is stored under a B-suffixed env name so it
        # can't collide with Account A's key (held by inspiring-cat). The
        # gemini CLI itself reads GEMINI_API_KEY, so we forward it on
        # subprocess invocation.
        self.api_key_b = os.environ.get("GEMINI_API_KEY_B", "")
        # Preferred model can be overridden via env. The fallback chain is
        # always walked starting from this model — anything after it in the
        # default chain is treated as a fallback target. If the override
        # isn't in the chain, we put it at the front.
        preferred = os.environ.get("GEMINI_MODEL_B", DEFAULT_MODEL_CHAIN[0])
        self.model_chain: list[str] = (
            [preferred] + [m for m in DEFAULT_MODEL_CHAIN if m != preferred]
        )

    async def _try_model(
        self, model: str, query: str, deadline_ms: int, env: dict
    ) -> tuple[AgentResponse, str]:
        """
        Run gemini CLI with a specific model. Returns (response, stderr_text)
        so the caller can inspect quota errors and fall through to the next
        model in the chain.
        """
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                "--skip-trust", "--yolo", "-o", "text",
                "-m", model,
                "--prompt", query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    self_confidence=0.0, error_class="binary_not_found",
                ),
                "",
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=deadline_ms / 1000,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    self_confidence=0.0, error_class="subprocess_timeout",
                ),
                "",
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        stderr_text = (stderr or b"").decode(errors="replace")
        if proc.returncode != 0:
            err_snippet = stderr_text[:1200]
            out_snippet = (stdout or b"").decode(errors="replace")[:300]
            log.warning(
                "gemini_b model=%s non-zero exit %s qlen=%d stdout=%r stderr=%r",
                model, proc.returncode, len(query), out_snippet, err_snippet,
            )
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=latency_ms, self_confidence=0.0,
                    error_class=f"exit_{proc.returncode}",
                ),
                stderr_text,
            )
        text = (stdout or b"").decode(errors="replace").strip()
        if not text:
            return (
                AgentResponse(
                    agent_id=self.agent_id, content=None, success=False,
                    latency_ms=latency_ms, self_confidence=0.0,
                    error_class="empty_output",
                ),
                stderr_text,
            )
        return (
            AgentResponse(
                agent_id=self.agent_id, content=text, success=True,
                latency_ms=latency_ms, self_confidence=0.55,
            ),
            stderr_text,
        )

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="disabled",
            )
        env = os.environ.copy()
        if self.api_key_b:
            env["GEMINI_API_KEY"] = self.api_key_b
        # Gemini CLI refuses to run when TERM lacks 256-color support and
        # NO_COLOR isn't set explicitly — the supervisord-spawned subprocess
        # inherits TERM=dumb which trips that check. Force a sane terminal
        # plus NO_COLOR so the CLI emits plain text.
        env.setdefault("TERM", "xterm-256color")
        env["NO_COLOR"] = "1"

        # Walk the chain: skip models we already know are exhausted in this
        # UTC day, try the rest until one succeeds or we run out. Total
        # attempts capped at deadline so we don't blow the per-agent budget
        # in the hive engine.
        per_model_deadline = max(int(deadline_ms / max(len(self.model_chain), 1)), 8000)
        attempted: list[str] = []
        last_response: AgentResponse | None = None
        for model in self.model_chain:
            if quota_state.is_exhausted(self.agent_id, model):
                log.info("gemini_b: skipping %s (quota exhausted)", model)
                continue
            attempted.append(model)
            response, stderr_text = await self._try_model(
                model, query, per_model_deadline, env,
            )
            if response.success:
                if model != self.model_chain[0]:
                    log.info("gemini_b: served via fallback model %s", model)
                return response
            last_response = response
            # Quota error: mark this model exhausted until UTC midnight and
            # try the next one in the chain.
            if _is_quota_error(stderr_text):
                quota_state.mark_exhausted_until_utc_midnight(
                    self.agent_id, model, reason="terminal_quota",
                )
                continue
            # Non-quota failure (timeout, network, malformed) — don't burn
            # the whole chain on a transient blip; bubble up immediately.
            return response
        if last_response is not None:
            return last_response
        # All models in the chain were marked exhausted before any attempt —
        # surface that explicitly so circuit breaker / dashboard can show it.
        return AgentResponse(
            agent_id=self.agent_id, content=None, success=False,
            latency_ms=0, self_confidence=0.0,
            error_class="all_models_quota_exhausted",
        )
