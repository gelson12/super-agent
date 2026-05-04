"""
Claude CLI agent pinned to Account B. Subprocess wrapper around
@anthropic-ai/claude-code with HOME rewritten to an account-scoped directory
so Account B's credentials never mix with Account A's (which lives in
inspiring-cat's container, not here).

Two execution modes:
  chat  (default) — claude --print, stdin prompt, fast text response
  admin (task_kind="admin") — claude -p, cwd=/workspace/super-agent,
        full tool access (Bash, Read, Edit, Write, git), same capability
        as inspiring-cat. Requires repo bootstrapped by bootstrap_repo.sh.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from app.models import AgentResponse

log = logging.getLogger("legion.agent.claude_b")

ACCOUNT_B_HOME = "/workspace/legion/claude-b"
REPO_CWD = "/workspace/super-agent"


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

        # Strip API key so Account B OAuth is used (not pay-per-token API)
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["HOME"] = ACCOUNT_B_HOME

        # Admin mode detected via [ADMIN] prefix injected by Telegram handler.
        # Uses claude -p with cwd=/workspace/super-agent (full tool access).
        # Chat mode uses claude --print (fast, text-only, no file/bash access).
        is_admin = query.startswith("[ADMIN]\n")
        clean_query = query[8:] if is_admin else query
        repo_available = os.path.isdir(REPO_CWD)

        if is_admin and repo_available:
            cmd = [self.binary, "-p", clean_query]
            cwd = REPO_CWD
            stdin_bytes = None
            log.info("claude_b admin mode: cwd=%s deadline=%.0fs", cwd, deadline_ms / 1000)
        else:
            cmd = [self.binary, "--print", "--output-format", "text"]
            cwd = None
            stdin_bytes = clean_query.encode()
            if is_admin and not repo_available:
                log.warning("claude_b: admin mode requested but %s not found — falling back to --print", REPO_CWD)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except FileNotFoundError:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="binary_not_found",
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
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
            latency_ms=latency_ms, self_confidence=0.85 if is_admin else 0.75,
        )
