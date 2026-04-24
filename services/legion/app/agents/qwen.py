"""
Qwen agent — wraps Alibaba's `@qwen-code/qwen-code` CLI (binary: `qwen`).

Pattern mirrors KimiAgent / ClaudeBAgent: CLI-subscription authed via a
one-time `qwen` login (captured into QWEN_SESSION_TOKEN base64 tarball and
restored on boot by app.healing.cli_creds). No API key required when using
the CLI subscription path; QWEN_API_KEY is a legacy fallback for DashScope.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from app.models import AgentResponse

log = logging.getLogger("legion.agent.qwen")


class QwenAgent:
    agent_id = "qwen"

    def __init__(self) -> None:
        self.enabled = os.environ.get("QWEN_ENABLED", "false").lower() == "true"
        self.binary = os.environ.get("QWEN_BINARY", "qwen")
        self.account_email = os.environ.get("QWEN_ACCOUNT_EMAIL", "")

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="disabled",
            )
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--prompt", query, "--output-format", "text",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "QWEN_NON_INTERACTIVE": "1"},
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
            log.warning("qwen non-zero exit %s: %s", proc.returncode, err)
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
            latency_ms=latency_ms, self_confidence=0.6,
        )
