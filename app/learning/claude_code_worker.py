"""
Claude Code CLI worker — subprocess wrapper + insight logger.

ask_claude_code(prompt) runs `claude -p "..."` inside /workspace,
giving the model access to actual files on disk (unlike API-only models).

log_claude_code_result() persists outcomes to /workspace/claude_code_insights.json
so Super Agent can learn which model wins which task type over time.
"""
import datetime
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

_INSIGHTS_FILE = Path("/workspace/claude_code_insights.json")
_TIMEOUT = 120
_POLL_INTERVAL = 2   # seconds between task-status polls
_POLL_TIMEOUT  = 130 # total poll budget (CLI timeout + 10s buffer)


# ── CLI worker HTTP helpers ───────────────────────────────────────────────────

def _cli_worker_url() -> str:
    return os.environ.get("CLI_WORKER_URL", "").rstrip("/")


def _submit_task(cli_url: str, task_type: str, payload: dict) -> str | None:
    """Submit a task to the CLI worker. Returns task_id or None on error."""
    try:
        data = json.dumps({"type": task_type, "payload": payload}).encode("utf-8")
        req  = urllib.request.Request(
            f"{cli_url}/tasks",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("task_id")
    except Exception:
        return None


def _poll_task(cli_url: str, task_id: str, timeout: int = _POLL_TIMEOUT) -> str:
    """Poll CLI worker until task is done/failed or timeout. Returns result string."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"{cli_url}/tasks/{task_id}", timeout=10
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                status = body.get("status")
                if status == "done":
                    return body.get("result") or "(no output)"
                if status == "failed":
                    return f"[claude_code: task failed — {body.get('error', 'unknown')}]"
        except Exception:
            pass
        time.sleep(_POLL_INTERVAL)
    return f"[claude_code: timed out waiting for CLI worker after {timeout}s]"


def ask_claude_code(prompt: str) -> str:
    """
    Run the Claude Code CLI non-interactively with the given prompt.
    Checks the shared ResponseCache first (TTL 1h) — on a hit the subprocess
    is skipped entirely, saving Pro quota.  On a miss the response is cached
    for future identical prompts.  Usage is recorded to pro_usage_tracker
    regardless of cache status.

    Returns the response string. Never raises — returns an error string
    so ThreadPoolExecutor competitors keep running on failure.
    """
    # ── Cache lookup ──────────────────────────────────────────────────────────
    try:
        from ..cache.response_cache import cache as _cache
        _hit = _cache.get(prompt, "PRO_CLI", ttl=3600)
        if _hit:
            try:
                from .pro_usage_tracker import record as _pro_record
                _pro_record(len(prompt), len(_hit), was_cached=True)
            except Exception:
                pass
            return _hit
    except Exception:
        pass

    # ── Gemini fallback when Pro daily limit is hit ───────────────────────────
    try:
        from .pro_router import is_pro_available
        if not is_pro_available():
            from .gemini_cli_worker import ask_gemini_cli
            return ask_gemini_cli(prompt)
    except Exception:
        pass

    # ── Route via CLI worker service (durable, survives API restarts) ─────────
    cli_url = _cli_worker_url()
    if cli_url:
        task_id = _submit_task(cli_url, "claude_pro", {"prompt": prompt})
        if task_id:
            output = _poll_task(cli_url, task_id)
        else:
            output = "[claude_code: failed to submit task to CLI worker]"

        if not output.startswith("["):
            try:
                from ..cache.response_cache import cache as _cache
                _cache.set(prompt, "PRO_CLI", output)
            except Exception:
                pass
            try:
                from .pro_usage_tracker import record as _pro_record
                _pro_record(len(prompt), len(output), was_cached=False)
            except Exception:
                pass
        return output

    # ── Direct subprocess fallback (no CLI worker configured / dev mode) ──────
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd="/workspace",
        )
        output = result.stdout.strip() or result.stderr.strip() or "(no output)"

        if not output.startswith("["):
            try:
                from ..cache.response_cache import cache as _cache
                _cache.set(prompt, "PRO_CLI", output)
            except Exception:
                pass

        try:
            from .pro_usage_tracker import record as _pro_record
            _pro_record(len(prompt), len(output), was_cached=False)
        except Exception:
            pass

        return output
    except subprocess.TimeoutExpired:
        try:
            from .gemini_cli_worker import ask_gemini_cli
            gemini_result = ask_gemini_cli(prompt)
            if not gemini_result.startswith("["):
                return gemini_result
        except Exception:
            pass
        return f"[claude_code: timed out after {_TIMEOUT}s]"
    except FileNotFoundError:
        return "[claude_code: claude CLI not found — set CLI_WORKER_URL or install claude CLI]"
    except Exception as e:
        return f"[claude_code error: {e}]"


def log_claude_code_result(
    prompt: str,
    response: str,
    was_winner: bool,
    agent_type: str,
) -> None:
    """
    Append one competition record to /workspace/claude_code_insights.json.
    Best-effort — never raises, never blocks the caller.
    Keeps the last 500 records to prevent unbounded growth.
    """
    try:
        records = (
            json.loads(_INSIGHTS_FILE.read_text())
            if _INSIGHTS_FILE.exists()
            else []
        )
        records.append({
            "ts": datetime.datetime.utcnow().isoformat(),
            "agent_type": agent_type,
            "prompt_preview": prompt[:200],
            "response_preview": response[:200],
            "was_winner": was_winner,
        })
        _INSIGHTS_FILE.write_text(json.dumps(records[-500:], indent=2))
    except Exception:
        pass
