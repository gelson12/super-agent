from __future__ import annotations

import asyncio
import logging
import os
import time

from app.models import AgentResponse

log = logging.getLogger("legion.agent.kimi")


class KimiAgent:
    agent_id = "kimi"

    def __init__(self) -> None:
        self.enabled = os.environ.get("KIMI_ENABLED", "false").lower() == "true"
        # Binary name per code.kimi.com install script. Confirm during P1 staging smoke
        # and update if upstream ships a different executable name.
        self.binary = os.environ.get("KIMI_BINARY", "kimi")

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id,
                content=None,
                success=False,
                latency_ms=0,
                self_confidence=0.0,
                error_class="disabled",
            )

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            "--print",
            "--output-format", "text",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "KIMI_NON_INTERACTIVE": "1"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=query.encode()),
                timeout=deadline_ms / 1000,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AgentResponse(
                agent_id=self.agent_id,
                content=None,
                success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0,
                error_class="subprocess_timeout",
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace")[:200]
            log.warning("kimi non-zero exit %s: %s", proc.returncode, err)
            return AgentResponse(
                agent_id=self.agent_id,
                content=None,
                success=False,
                latency_ms=latency_ms,
                self_confidence=0.0,
                error_class=f"exit_{proc.returncode}",
            )

        text = (stdout or b"").decode(errors="replace").strip()
        if not text:
            return AgentResponse(
                agent_id=self.agent_id,
                content=None,
                success=False,
                latency_ms=latency_ms,
                self_confidence=0.0,
                error_class="empty_output",
            )

        return AgentResponse(
            agent_id=self.agent_id,
            content=text,
            success=True,
            latency_ms=latency_ms,
            self_confidence=0.6,
        )
