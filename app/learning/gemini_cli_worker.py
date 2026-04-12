"""
Gemini CLI Worker — backup for when Claude Pro CLI hits its daily limit.

Uses the Google Gemini CLI (free tier via Google account OAuth) as a
zero-cost fallback for nightly reviews, health checks, and voting calls.

Free tier:  ~1,500 requests/day with Gemini 2.5 Pro via Google account
Install:    npm install -g @google/gemini-cli
Auth:       gemini auth login  (Google account OAuth, no credit card)
Creds:      /root/.gemini/credentials.json  (decoded from GEMINI_SESSION_TOKEN)

Usage is tracked in pro_usage_tracker with model="GEMINI_CLI" so the
/pro-usage endpoint shows combined quota consumption.

Never raises — returns an error string on failure so callers can handle it.
"""
import json
import os
import subprocess
import time
import urllib.request

_TIMEOUT    = 120
_CREDS_DIR  = "/root/.gemini"
_POLL_INTERVAL = 2
_POLL_TIMEOUT  = 130


def _cli_worker_url() -> str:
    return os.environ.get("CLI_WORKER_URL", "").rstrip("/")


def _submit_task(cli_url: str, task_type: str, payload: dict) -> str | None:
    try:
        data = json.dumps({"type": task_type, "payload": payload}).encode("utf-8")
        req  = urllib.request.Request(
            f"{cli_url}/tasks",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("task_id")
    except Exception:
        return None


def _poll_task(cli_url: str, task_id: str, timeout: int = _POLL_TIMEOUT) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{cli_url}/tasks/{task_id}", timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("status") == "done":
                    return body.get("result") or "(no output)"
                if body.get("status") == "failed":
                    return f"[gemini_cli: task failed — {body.get('error', 'unknown')}]"
        except Exception:
            pass
        time.sleep(_POLL_INTERVAL)
    return f"[gemini_cli: timed out waiting for CLI worker after {timeout}s]"


def is_gemini_cli_available() -> bool:
    """Return True if Gemini CLI is available — checks CLI worker health endpoint
    first (since Gemini CLI lives on inspiring-cat, not super-agent), falls back
    to local binary check."""
    # Try CLI worker health endpoint first (the actual source of truth)
    cli_url = _cli_worker_url()
    if cli_url:
        try:
            import urllib.request
            import json
            with urllib.request.urlopen(f"{cli_url}/health", timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("gemini_available", False)
        except Exception:
            pass

    # Fallback: local binary check (only works if gemini is installed locally)
    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HOME": "/root"},
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def ask_gemini_cli(prompt: str) -> str:
    """
    Run the Google Gemini CLI non-interactively with the given prompt.

    Returns the response string. Never raises — returns an error string
    prefixed with [ so callers know it's an error (same contract as ask_claude_code).
    """
    def _track_gemini(state, task=""):
        try:
            from .agent_status_tracker import mark_working, mark_done, mark_sick
            if state == "working": mark_working("Gemini CLI", task)
            elif state == "done": mark_done("Gemini CLI")
            elif state == "sick": mark_sick("Gemini CLI")
        except Exception:
            pass

    try:
        # Try response cache first (same TTL as Claude CLI cache)
        try:
            from ..cache.response_cache import cache as _cache
            _hit = _cache.get(prompt, "GEMINI_CLI", ttl=3600)
            if _hit:
                return _hit
        except Exception:
            pass

        _track_gemini("working", prompt[:100])

        # ── Route via CLI worker (durable) ───────────────────────────────────
        cli_url = _cli_worker_url()
        if cli_url:
            task_id = _submit_task(cli_url, "gemini_cli", {"prompt": prompt})
            if task_id:
                output = _poll_task(cli_url, task_id)
            else:
                output = "[gemini_cli: failed to submit task to CLI worker]"
        else:
            # ── Direct subprocess fallback ────────────────────────────────────
            try:
                result = subprocess.run(
                    ["gemini", "--prompt", prompt],
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT,
                    cwd="/workspace",
                    env={**os.environ, "HOME": "/root"},
                )
                output = result.stdout.strip() or result.stderr.strip() or "(no output)"
            except subprocess.TimeoutExpired:
                return f"[gemini_cli: timed out after {_TIMEOUT}s]"
            except FileNotFoundError:
                return "[gemini_cli: gemini CLI not found — set CLI_WORKER_URL or install gemini CLI]"
            except Exception as e:
                return f"[gemini_cli error: {e}]"

        # Cache and track on success; alert on failure
        if not output or output.startswith("["):
            _track_gemini("sick")
        else:
            _track_gemini("done")

        if output and not output.startswith("["):
            try:
                from ..cache.response_cache import cache as _cache
                _cache.set(prompt, "GEMINI_CLI", output)
            except Exception:
                pass
            try:
                from .pro_usage_tracker import record as _pro_record
                _pro_record(len(prompt), len(output), was_cached=False)
            except Exception:
                pass
        else:
            # Gemini returned an error — alert that Anthropic credits are next fallback
            try:
                from ..alerts.notifier import alert_gemini_cli_down
                alert_gemini_cli_down(error=output)
            except Exception:
                pass

        return output

    except Exception as e:
        return f"[gemini_cli error: {e}]"
