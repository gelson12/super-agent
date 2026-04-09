"""
CLI Worker Task Runner — executes CLI subprocesses and writes results to Postgres.

Task types:
  claude_pro    → claude -p "{prompt}"          (cwd=/workspace, timeout=120s)
  gemini_cli    → gemini --prompt "{prompt}"    (cwd=/workspace, timeout=120s)
  claude_auth   → claude auth status            (timeout=15s)
  claude_probe  → claude --version              (timeout=15s)
  gemini_probe  → gemini --version              (timeout=15s)
  flutter_build → flutter build apk --release  (cwd=/workspace, timeout=600s)

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
}

_WORKSPACE = "/workspace"
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


def _run_subprocess(cmd: list[str], cwd: str | None, timeout: int) -> str:
    """Run a subprocess and return its output string. Never raises."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=_CLI_ENV,
        )
        return (result.stdout or result.stderr or "(no output)").strip()
    except subprocess.TimeoutExpired:
        return f"[cli_worker: timed out after {timeout}s]"
    except FileNotFoundError:
        return f"[cli_worker: binary not found — {cmd[0]}]"
    except Exception as e:
        return f"[cli_worker error: {e}]"


def execute_task(task_id: str, task_type: str, payload: dict) -> None:
    """
    Execute one CLI task end-to-end: mark running → run subprocess → mark done/failed.
    Called from the background worker loop. Never raises.
    """
    try:
        _mark_running(task_id)
        timeout = _TIMEOUTS.get(task_type, 120)

        if task_type == "claude_pro":
            prompt = payload.get("prompt", "")
            result = _run_subprocess(["claude", "-p", prompt], _WORKSPACE, timeout)

        elif task_type == "gemini_cli":
            prompt = payload.get("prompt", "")
            result = _run_subprocess(["gemini", "--prompt", prompt], _WORKSPACE, timeout)

        elif task_type == "claude_auth":
            result = _run_subprocess(["claude", "auth", "status"], None, timeout)

        elif task_type == "claude_probe":
            result = _run_subprocess(["claude", "--version"], None, timeout)

        elif task_type == "gemini_probe":
            result = _run_subprocess(["gemini", "--version"], None, timeout)

        elif task_type == "flutter_build":
            extra_args = payload.get("args", [])
            result = _run_subprocess(
                ["flutter", "build", "apk", "--release"] + extra_args,
                payload.get("cwd", _WORKSPACE),
                timeout,
            )

        else:
            result = f"[cli_worker: unknown task type '{task_type}']"

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
        if task_type == "claude_pro":
            prompt = payload.get("prompt", "")
            result = _run_subprocess(["claude", "-p", prompt], _WORKSPACE, timeout)

        elif task_type == "gemini_cli":
            prompt = payload.get("prompt", "")
            result = _run_subprocess(["gemini", "--prompt", prompt], _WORKSPACE, timeout)

        elif task_type == "claude_auth":
            result = _run_subprocess(["claude", "auth", "status"], None, timeout)

        elif task_type == "claude_probe":
            result = _run_subprocess(["claude", "--version"], None, timeout)

        elif task_type == "gemini_probe":
            result = _run_subprocess(["gemini", "--version"], None, timeout)

        elif task_type == "flutter_build":
            extra_args = payload.get("args", [])
            result = _run_subprocess(
                ["flutter", "build", "apk", "--release"] + extra_args,
                payload.get("cwd", _WORKSPACE),
                timeout,
            )

        else:
            result = f"[cli_worker: unknown task type '{task_type}']"

        _mark_done(task_id, result)

    except Exception as e:
        try:
            _mark_failed(task_id, str(e))
        except Exception:
            pass
