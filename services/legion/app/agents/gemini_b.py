from __future__ import annotations

import asyncio
import logging
import os
import time

from app.models import AgentResponse

log = logging.getLogger("legion.agent.gemini_b")


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

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="disabled",
            )
        start = time.monotonic()
        env = os.environ.copy()
        if self.api_key_b:
            env["GEMINI_API_KEY"] = self.api_key_b
        # Gemini CLI refuses to run when TERM lacks 256-color support and
        # NO_COLOR isn't set explicitly — the supervisord-spawned subprocess
        # inherits TERM=dumb which trips that check. Force a sane terminal
        # plus NO_COLOR so the CLI emits plain text.
        env.setdefault("TERM", "xterm-256color")
        env["NO_COLOR"] = "1"
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--skip-trust", "--prompt", query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="binary_not_found",
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=deadline_ms / 1000,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="subprocess_timeout",
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace")[:200]
            log.warning("gemini_b non-zero exit %s: %s", proc.returncode, err)
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=latency_ms, self_confidence=0.0,
                error_class=f"exit_{proc.returncode}",
            )
        text = (stdout or b"").decode(errors="replace").strip()
        if not text:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=latency_ms, self_confidence=0.0,
                error_class="empty_output",
            )
        return AgentResponse(
            agent_id=self.agent_id, content=text, success=True,
            latency_ms=latency_ms, self_confidence=0.55,
        )
