"""
Claude CLI agent pinned to Account B. Subprocess wrapper around
@anthropic-ai/claude-code with HOME rewritten to an account-scoped directory
so Account B's credentials never mix with Account A's (which lives in
inspiring-cat's container, not here).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from app.models import AgentResponse

log = logging.getLogger("legion.agent.claude_b")

ACCOUNT_B_HOME = "/workspace/legion/claude-b"


class ClaudeBAgent:
    agent_id = "claude_b"

    def __init__(self) -> None:
        _dual = os.environ.get("DUAL_ACCOUNT_ENABLED", "false").lower() == "true"
        self.account_email = os.environ.get("CLAUDE_ACCOUNT_B_EMAIL", "")
        self.binary = os.environ.get("CLAUDE_BINARY", "claude")
        # Auto-disable if credentials file doesn't exist — avoids 369-failure loops
        _creds = os.path.join(ACCOUNT_B_HOME, ".claude", ".credentials.json")
        self.enabled = _dual and os.path.isfile(_creds)

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="disabled",
            )
        start = time.monotonic()
        env = {**os.environ, "HOME": ACCOUNT_B_HOME}
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--print", "--output-format", "text",
                stdin=asyncio.subprocess.PIPE,
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
                proc.communicate(input=query.encode()),
                timeout=deadline_ms / 1000,
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
            log.warning("claude_b exit %s: %s", proc.returncode, err)
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
            latency_ms=latency_ms, self_confidence=0.75,
        )
