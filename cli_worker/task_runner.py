"""
CLI Worker Task Runner — executes CLI subprocesses and writes results to Postgres.

Task types:
  claude_pro    → claude -p "{prompt}"          (cwd=/workspace, timeout=120s)
  gemini_cli    → gemini --prompt "{prompt}"    (cwd=/workspace, timeout=120s)
  claude_auth   → claude auth status            (timeout=15s)
  claude_probe  → claude --version              (timeout=15s)
  gemini_probe  → gemini --version              (timeout=15s)
  flutter_build → flutter build apk --release  (cwd=/workspace, timeout=600s)
  shell         → bash -c "{command}"           (cwd=/workspace, timeout=60s)

Each task: pending → running → done | failed
Never raises — writes error string on any exception.
"""
import json
import os
import subprocess
import threading
import psycopg2
from datetime import datetime, timezone


_TIMEOUTS = {
    "claude_pro":    120,
    "gemini_cli":    120,
    "claude_auth":   15,
    "claude_probe":  15,
    "gemini_probe":  15,
    "flutter_build": 600,
    "shell":         60,   # generic shell command — 60s for vault MCP cold-starts (~15s Xvfb+Obsidian + SSE overhead)
}

_WORKSPACE = "/workspace"
_REPO_WORKSPACE = "/workspace/super-agent"  # cloned repo — contains CLAUDE.md + GEMINI.md


def _repo_cwd() -> str:
    """Return /workspace/super-agent if already cloned, else /workspace.
    Claude CLI and Gemini CLI read their context files (CLAUDE.md / GEMINI.md)
    from the cwd, so running inside the repo root gives them full architectural
    awareness on every invocation."""
    import os as _os
    return _REPO_WORKSPACE if _os.path.isdir(_REPO_WORKSPACE) else _WORKSPACE


def _read_gemini_md() -> str:
    """Read GEMINI.md from the repo root and return its content.
    Gemini CLI does not guarantee auto-loading of GEMINI.md (unlike Claude CLI
    with CLAUDE.md), so we prepend it manually to every prompt.
    Returns empty string silently if the file is missing or unreadable."""
    try:
        path = os.path.join(_repo_cwd(), "GEMINI.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


# Strip ANTHROPIC_API_KEY so claude CLI uses OAuth (claude.ai Pro subscription)
# instead of the API key. When ANTHROPIC_API_KEY is present, claude -p always
# prefers it over OAuth — causing "Credit balance is too low" even when Pro
# credentials are valid in /root/.claude/.credentials.json
_CLI_ENV = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
_CLI_ENV["HOME"] = "/root"


def _conn():
    url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def _mark_running(task_id: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cli_tasks SET status='running', started_at=NOW() WHERE id=%s",
                (task_id,)
            )
        conn.commit()


def _mark_done(task_id: str, result: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cli_tasks SET status='done', result=%s, finished_at=NOW() WHERE id=%s",
                (result, task_id)
            )
        conn.commit()


def _mark_failed(task_id: str, error: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cli_tasks SET status='failed', error=%s, finished_at=NOW() WHERE id=%s",
                (error, task_id)
            )
        conn.commit()


# Phrases that indicate the Claude CLI session token has expired.
# When a claude_pro task returns any of these, full_recovery_chain() is triggered
# in a background thread so future tasks can succeed without manual intervention.
_AUTH_ERROR_PHRASES = (
    "not logged in",
    "not authenticated",
    "authentication required",
    "please run claude login",
    "please login",
    "run claude login",
    "invalid api key",
    "credit balance is too low",
    "unauthenticated",
    "401",
    "session expired",
    "token expired",
    "oauth",
    "login required",
    "no credentials",
)

_MCP_PERMISSION_PHRASES = (
    "need your permission to use",
    "permission prompts for",
    "please approve",
    "approve both",
    "approve the tool",
    "approve this tool",
    "grant permission",
    "you should see permission prompts",
    "mcp tool permissions",
)

_MCP_TOOL_FAILURE_PHRASES = (
    "mcp error",
    "mcp tool error",
    "connection refused",
    "econnrefused",
    "n8n error",
    "n8n is unreachable",
    "n8n is not reachable",
    "tool execution failed",
    "tool call failed",
    "failed to execute tool",
    "could not connect to",
    "timed out waiting for",
    "unable to reach n8n",
    "workflow execution failed",
    "502 bad gateway",
    "503 service unavailable",
    "socket hang up",
    "etimedout",
    "enotfound",
)

def _run_subprocess(cmd: list[str], cwd: str | None, timeout: int) -> str:
    """Run a subprocess and return its output string. Never raises."""
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,  # /dev/null → immediate EOF, no pipe setup, no 3-second CLI wait
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=_CLI_ENV,
        )
        output = (result.stdout or result.stderr or "(no output)").strip()
        # If MCP permission phrases still appear, extract content lines only
        lower = output.lower()
        if any(p in lower for p in _MCP_PERMISSION_PHRASES):
            lines = output.splitlines()
            clean_lines = [l for l in lines if not any(p in l.lower() for p in _MCP_PERMISSION_PHRASES)]
            clean = "\n".join(clean_lines).strip()
            if clean and len(clean) > 20:
                return clean
        # Detect MCP tool execution failures — signal with "[" prefix for upstream fallback
        if any(p in lower for p in _MCP_TOOL_FAILURE_PHRASES):
            return f"[mcp_tool_error: {output[:200]}]"
        return output
    except subprocess.TimeoutExpired:
        return f"[cli_worker: timed out after {timeout}s]"
    except FileNotFoundError:
        return f"[cli_worker: binary not found — {cmd[0]}]"
    except Exception as e:
        return f"[cli_worker error: {e}]"


def _run_shell(command: str, cwd: str, timeout: int) -> str:
    """Run an arbitrary bash command and return output. Never raises."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=_CLI_ENV,
        )
        return proc.stdout.strip() or proc.stderr.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[cli_worker: shell timed out after {timeout}s]"
    except Exception as e:
        return f"[cli_worker: shell error — {e}]"


def _trigger_recovery_bg() -> None:
    """
    Trigger full_recovery_chain() in a background thread.
    Called when a claude_pro task returns an auth error — ensures the CLI session
    is healed automatically without blocking the current task result.
    """
    def _run():
        try:
            import sys as _sys
            _sys.path.insert(0, "/app")
            from app.learning.cli_auto_login import full_recovery_chain
            import logging
            logger = logging.getLogger("task_runner.recovery")
            logger.info("[recovery] Auth error detected — starting full_recovery_chain()")
            ok = full_recovery_chain()
            logger.info(f"[recovery] full_recovery_chain() result: {'success' if ok else 'failed'}")
        except Exception as _e:
            import logging
            logging.getLogger("task_runner.recovery").error(f"[recovery] Exception: {_e}")

    t = threading.Thread(target=_run, daemon=True, name="cli-recovery")
    t.start()


def _dispatch(task_type: str, payload: dict, timeout: int) -> str:
    """Central dispatch — maps task type to execution. Returns result string."""
    if task_type == "claude_pro":
        # Circuit breaker: fail-fast when recovery is already running so queued
        # tasks don't each burn 120s waiting to hit the same auth error.
        # Primary check: in-memory _recovery_running Event (immediate, no file I/O).
        try:
            import sys as _sys_cb
            _sys_cb.path.insert(0, "/app")
            from app.learning.cli_auto_login import _recovery_running
            if _recovery_running.is_set():
                print("[task_runner] CLI circuit breaker: _recovery_running set — skipping claude_pro", flush=True)
                return "[cli_worker: CLI auth recovery in progress — skipping claude_pro task]"
        except Exception:
            pass
        # Secondary check: file-based CLI_DOWN flag (works across restarts).
        from pathlib import Path as _Path
        _cli_down_flag = _Path(os.environ.get("FLAG_DIR", "/workspace")) / ".pro_cli_down"
        if _cli_down_flag.exists():
            import time as _t
            if (_t.time() - _cli_down_flag.stat().st_mtime) < 600:  # respect 10-min TTL
                return "[cli_worker: CLI_DOWN — skipped subprocess, recovery in progress]"

        # cwd = repo root so CLAUDE.md is auto-loaded by Claude CLI on every call
        result = _run_subprocess(["claude", "-p", payload.get("prompt", "")], _repo_cwd(), timeout)
        # Detect expired session — kick off recovery in background so next task succeeds
        _lower = result.lower()
        if any(p in _lower for p in _AUTH_ERROR_PHRASES):
            print(f"[task_runner] Auth error detected in claude_pro output — triggering recovery. "
                  f"Preview: {result[:120]}", flush=True)
            _trigger_recovery_bg()
        return result

    elif task_type == "gemini_cli":
        # Prepend GEMINI.md explicitly — Gemini CLI does not guarantee auto-loading
        # of GEMINI.md the way Claude CLI auto-loads CLAUDE.md.
        gemini_ctx = _read_gemini_md()
        raw_prompt = payload.get("prompt", "")
        full_prompt = f"{gemini_ctx}\n\n---\n\n{raw_prompt}" if gemini_ctx else raw_prompt
        return _run_subprocess(["gemini", "--prompt", full_prompt], _repo_cwd(), timeout)

    elif task_type == "claude_auth":
        return _run_subprocess(["claude", "auth", "status"], None, timeout)

    elif task_type == "claude_probe":
        return _run_subprocess(["claude", "--version"], None, timeout)

    elif task_type == "gemini_probe":
        return _run_subprocess(["gemini", "--version"], None, timeout)

    elif task_type == "flutter_build":
        extra_args = payload.get("args", [])
        return _run_subprocess(
            ["flutter", "build", "apk", "--release"] + extra_args,
            payload.get("cwd", _WORKSPACE),
            timeout,
        )

    elif task_type == "shell":
        command = payload.get("command", "").strip()
        if not command:
            return "[cli_worker: shell task requires 'command' in payload]"
        return _run_shell(command, payload.get("cwd", _WORKSPACE), timeout)

    else:
        return f"[cli_worker: unknown task type '{task_type}']"


def execute_task(task_id: str, task_type: str, payload: dict) -> None:
    """
    Execute one CLI task end-to-end: mark running → run subprocess → mark done/failed.
    Called from the background worker loop. Never raises.
    """
    try:
        _mark_running(task_id)
        timeout = _TIMEOUTS.get(task_type, 120)
        result = _dispatch(task_type, payload, timeout)
        _mark_done(task_id, result)
    except Exception as e:
        try:
            _mark_failed(task_id, str(e))
        except Exception:
            pass


def fetch_pending_task() -> dict | None:
    """
    Atomically claim one pending task (SELECT FOR UPDATE SKIP LOCKED).
    Returns the task dict or None if queue is empty.
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, type, payload
                    FROM   cli_tasks
                    WHERE  status = 'pending'
                    ORDER  BY created_at
                    LIMIT  1
                    FOR UPDATE SKIP LOCKED
                """)
                row = cur.fetchone()
                if not row:
                    return None
                task_id, task_type, payload = row
                # Mark as running atomically inside the same transaction
                cur.execute(
                    "UPDATE cli_tasks SET status='running', started_at=NOW() WHERE id=%s",
                    (str(task_id),)
                )
            conn.commit()
            return {
                "id": str(task_id),
                "type": task_type,
                "payload": payload if isinstance(payload, dict) else json.loads(payload or "{}"),
            }
    except Exception:
        return None


def _push_result_to_shared_memory(task_type: str, payload: dict, result: str) -> None:
    """
    Fire-and-forget: after a successful CLI Pro / shell task, extract the
    prompt + result as a shared memory entry so all models benefit from it.

    Only fires for claude_pro tasks with substantive results (>200 chars).
    Runs in the calling thread (already background from main worker loop).
    Never raises.
    """
    try:
        if task_type not in ("claude_pro", "claude_auth") or len(result) < 200:
            return
        prompt = payload.get("prompt", "")
        if not prompt:
            return
        import sys
        sys.path.insert(0, "/app")
        from app.memory.vector_memory import ingest_external_memory, extract_and_store_insights
        # Store the raw exchange
        ingest_external_memory(
            content=f"CLI Pro task — Q: {prompt[:300]} A: {result[:400]}",
            memory_type="fact",
            importance=3,
            source="cli_pro",
            session_id="cli_pro_shared",
        )
        # Also fire Haiku distillation (runs async in daemon thread)
        extract_and_store_insights(
            message=prompt,
            response=result,
            model="CLI_PRO",
            session_id="cli_pro_shared",
            source="auto_extract",
        )
    except Exception:
        pass


def run_task_from_record(task: dict) -> None:
    """Execute a task that was already marked running by fetch_pending_task."""
    task_id   = task["id"]
    task_type = task["type"]
    payload   = task["payload"]
    timeout   = _TIMEOUTS.get(task_type, 120)

    try:
        result = _dispatch(task_type, payload, timeout)
        _mark_done(task_id, result)
        # Contribute CLI Pro results to the shared cross-model memory store
        _push_result_to_shared_memory(task_type, payload, result)
    except Exception as e:
        try:
            _mark_failed(task_id, str(e))
        except Exception:
            pass
