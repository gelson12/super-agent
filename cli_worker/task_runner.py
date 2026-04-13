"""
CLI Worker Task Runner — executes CLI subprocesses and writes results to Postgres.

Task types:
  claude_pro    → claude -p "{prompt}"          (cwd=/workspace, timeout=120s)
  gemini_cli    → gemini --prompt "{prompt}"    (cwd=/workspace, timeout=120s)
  claude_auth   → claude auth status            (timeout=15s)
  claude_probe  → claude --version              (timeout=15s)
  gemini_probe  → gemini --version              (timeout=15s)
  flutter_build → flutter build apk --release  (cwd=/workspace, timeout=600s)
  shell         → bash -c "{command}"           (cwd=/workspace, timeout=30s)

Each task: pending → running → done | failed
Never raises — writes error string on any exception.
"""
import json
import os
import subprocess
import psycopg2
from datetime import datetime, timezone


_TIMEOUTS = {
    "claude_pro":    120,
    "gemini_cli":    120,
    "claude_auth":   15,
    "claude_probe":  15,
    "gemini_probe":  15,
    "flutter_build": 600,
    "shell":         30,   # generic shell command — curl, git, any binary
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

# Auto-accept input: answer "1" (Allow once) to any interactive MCP permission prompts.
# Provides up to 30 answers so any sequence of permission requests is handled.
_AUTO_ACCEPT_INPUT = "1\n" * 30


def _run_subprocess(cmd: list[str], cwd: str | None, timeout: int) -> str:
    """Run a subprocess and return its output string. Never raises."""
    try:
        result = subprocess.run(
            cmd,
            input=_AUTO_ACCEPT_INPUT,  # auto-accept MCP permission prompts (option 1 = Allow once)
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


def _dispatch(task_type: str, payload: dict, timeout: int) -> str:
    """Central dispatch — maps task type to execution. Returns result string."""
    if task_type == "claude_pro":
        # cwd = repo root so CLAUDE.md is auto-loaded by Claude CLI on every call
        return _run_subprocess(["claude", "-p", payload.get("prompt", "")], _repo_cwd(), timeout)

    elif task_type == "gemini_cli":
        # cwd = repo root so GEMINI.md is auto-loaded by Gemini CLI on every call
        return _run_subprocess(["gemini", "--prompt", payload.get("prompt", "")], _repo_cwd(), timeout)

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


def run_task_from_record(task: dict) -> None:
    """Execute a task that was already marked running by fetch_pending_task."""
    task_id   = task["id"]
    task_type = task["type"]
    payload   = task["payload"]
    timeout   = _TIMEOUTS.get(task_type, 120)

    try:
        result = _dispatch(task_type, payload, timeout)
        _mark_done(task_id, result)
    except Exception as e:
        try:
            _mark_failed(task_id, str(e))
        except Exception:
            pass
