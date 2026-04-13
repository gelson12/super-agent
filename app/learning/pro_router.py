"""
Pro-first router — uses Claude.ai Pro subscription (via CLI) as the PRIMARY
model for ALL Claude calls, 100% of weekly allowance used before any fallback.

ANTHROPIC_API_KEY is used ONLY during two temporary blocking conditions:

  DAILY limit   — "daily limit reached, resets in 3h"
    Cause:  Today's message cap hit. Weekly quota is intact.
    Action: Parse reset hours from message. Use API until then + 15min buffer.
    Flag:   .pro_daily  (TTL = parsed hours from message, default 4h)
    After:  Pro resumes automatically.

  BURST throttle — "too many requests, try again shortly"
    Cause:  Momentary server-side overload. No quota consumed.
    Action: Use API for 30 minutes.
    Flag:   .pro_burst  (TTL 30 min)
    After:  Pro resumes automatically.

Weekly limits are intentionally IGNORED — they do not block Pro usage.
Super Agent uses 100% of the weekly Pro allowance before touching the API key.

Public API:
    try_pro(prompt, system="")   → str | None  (None = use API fallback)
    is_pro_available()           → bool
    reset_pro_flag()             → clears all flags
    get_status()                 → dict (for /credits/pro-status)
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import datetime
from pathlib import Path

# ── Per-request progress event queue ─────────────────────────────────────────
# try_pro() queues UI events (timeout retries, self-healing milestones) here.
# The SSE generator in main.py drains them AFTER dispatch() returns so they
# appear in the user's thinking-step bubble before the response streams in.
_thread_events = threading.local()


def _queue_progress(msg: str) -> None:
    """Queue a progress message for the current request's SSE stream."""
    if not hasattr(_thread_events, "events"):
        _thread_events.events = []
    _thread_events.events.append(msg)


def drain_progress_events() -> list:
    """Return and clear all queued progress events for this thread."""
    events = list(getattr(_thread_events, "events", []))
    _thread_events.events = []
    return events


def _cli_worker_url() -> str:
    return os.environ.get("CLI_WORKER_URL", "").rstrip("/")


def _submit_and_poll(task_type: str, payload: dict, timeout: int = 30) -> str | None:
    """Submit a task to CLI worker and poll for result. Returns result string or None."""
    cli_url = _cli_worker_url()
    if not cli_url:
        return None
    try:
        data = json.dumps({"type": task_type, "payload": payload}).encode("utf-8")
        req  = urllib.request.Request(
            f"{cli_url}/tasks",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            task_id = json.loads(resp.read().decode("utf-8")).get("task_id")
        if not task_id:
            _log(f"CLI worker returned no task_id for {task_type}")
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            with urllib.request.urlopen(f"{cli_url}/tasks/{task_id}", timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("status") == "done":
                    return body.get("result") or ""
                if body.get("status") == "failed":
                    _err = body.get("error", "unknown")
                    _log(f"CLI worker task {task_id} FAILED: {str(_err)[:200]}")
                    return f"[CLI worker error: {str(_err)[:200]}]"
            time.sleep(2)
        _log(f"CLI worker task {task_id} TIMED OUT after {timeout}s")
        return None
    except urllib.error.HTTPError as e:
        _log(f"CLI worker HTTP {e.code} on {task_type}: {str(e)[:200]}")
        return None
    except urllib.error.URLError as e:
        _log(f"CLI worker unreachable ({task_type}): {e.reason}")
        return None
    except Exception as e:
        _log(f"CLI worker error ({task_type}): {str(e)[:200]}")
        return None

_FLAG_DIR      = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
_DAILY_FLAG    = _FLAG_DIR / ".pro_daily"
_BURST_FLAG    = _FLAG_DIR / ".pro_burst"
_CLI_DOWN_FLAG = _FLAG_DIR / ".pro_cli_down"
# Tracks how many consecutive times we've "restored" credentials but CLI still fails.
# When this exceeds _RESTORE_ATTEMPT_LIMIT, we escalate to full Playwright recovery.
_RESTORE_COUNT_FLAG = _FLAG_DIR / ".pro_restore_count"
_RESTORE_ATTEMPT_LIMIT = 2  # after 2 failed restores, escalate to Playwright auto-login

_DEFAULT_DAILY_TTL = 4 * 3600   # fallback if reset time can't be parsed from message
_BURST_TTL         = 30 * 60    # 30 minutes
_CLI_DOWN_TTL      = 10 * 60    # 10 minutes — watchdog will clear sooner if CLI recovers
_RESET_BUFFER      = 15 * 60    # 15-min buffer added to parsed reset time

# Dynamic timeouts — scale with prompt complexity so large tasks never time out.
# Thresholds are character counts of the FULL prompt (system + user combined).
_TIMEOUT_SMALL  = 180   # < 1 500 chars  — quick chat / trivial query
_TIMEOUT_MEDIUM = 360   # 1 500 – 4 000  — moderate complexity (n8n, builds, agents)
_TIMEOUT_LARGE  = 480   # > 4 000 chars  — big analysis, multi-step agent planning

# Back-compat alias used elsewhere in this file
_TIMEOUT = _TIMEOUT_SMALL


def _dynamic_timeout(prompt: str) -> int:
    """Return the right timeout based on prompt length."""
    n = len(prompt)
    if n > 4000:
        return _TIMEOUT_LARGE
    if n > 1500:
        return _TIMEOUT_MEDIUM
    return _TIMEOUT_SMALL

# ── Phrase banks ───────────────────────────────────────────────────────────────

# MCP PERMISSION REQUEST: Claude is asking user to approve MCP tool use interactively.
# This happens when settings.json hasn't taken effect yet or is in an unsupported format.
# Treat as a non-response — discard and fall through to Gemini/LangGraph so the user
# never sees "please approve mcp__..." text in the chat.
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

# DAILY: today's cap hit — message always contains a reset time
_DAILY_PHRASES = (
    "daily limit",
    "daily usage limit",
    "daily message limit",
    "daily cap",
    "resets in",
    "resets at",
    "limit resets",
    "try again in",
    "available again in",
)

# BURST: momentary server overload — no quota consumed
_BURST_PHRASES = (
    "too many requests",
    "rate limit",
    "rate limited",
    "slow down",
    "overloaded",
    "high demand",
    "try again shortly",
    "try again later",
    "temporarily unavailable",
    "service unavailable",
    "capacity",
    "please wait",
)

# CLI DOWN: auth failure, token expired, not installed — set CLI_DOWN flag (10 min TTL)
# Watchdog probes every 5 min and clears flag automatically on recovery.
_CLI_DOWN_PHRASES = (
    "authentication required",
    "not authenticated",
    "please log in",
    "please run claude login",
    "run claude login",
    "session expired",
    "token expired",
    "invalid token",
    "login required",
    "not logged in",
    "auth failed",
    "invalid credentials",
    "invalid authentication credentials",
    "authentication_error",
    "credentials expired",
    "no valid credentials",
    # NOTE: "credentials" alone was too broad — matched file paths like
    # .credentials.json. "unauthorized" and "forbidden" removed because
    # they match too many non-auth contexts (e.g. Railway API 403).
)

# Weekly / billing / upgrade phrases — explicitly IGNORED, no flag set
_IGNORED_PHRASES = (
    "upgrade your plan",
    "upgrade to continue",
    "out of credits",
    "monthly usage limit",
    "subscription limit",
    "billing",
    "no remaining",
)

# Credit / balance errors — API key has no credits; return None so Gemini is tried
_CREDIT_ERROR_PHRASES = (
    "credit balance is too low",
    "credit balance",
    "insufficient credits",
    "insufficient funds",
    "payment required",
    "no credits",
    "credits remaining",
    "credits left",
)

# MCP tool execution failures — CLI returned exit 0 but output describes a tool error.
# When detected, return None so the caller falls through to Python tools.
_MCP_TOOL_ERROR_PHRASES = (
    "mcp error",
    "mcp tool error",
    "mcp_tool_error",
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


# ── Reset time parser ──────────────────────────────────────────────────────────

def _parse_reset_seconds(text: str) -> int:
    """
    Extract reset duration from an error message and return seconds.
    Handles: "resets in 3h", "resets in 2 hours", "try again in 45 minutes",
             "resets in 1h 30m", "available again in 2 hours 15 minutes"
    Returns parsed seconds + 15-min buffer, or _DEFAULT_DAILY_TTL if unparseable.
    """
    lower = text.lower()
    total = 0.0
    matched = False

    m = re.search(r'(\d+(?:\.\d+)?)\s*h(?:our[s]?)?', lower)
    if m:
        total += float(m.group(1)) * 3600
        matched = True

    m = re.search(r'(\d+(?:\.\d+)?)\s*m(?:in(?:ute[s]?)?)?', lower)
    if m:
        total += float(m.group(1)) * 60
        matched = True

    if matched and total > 0:
        return int(total) + _RESET_BUFFER
    return _DEFAULT_DAILY_TTL


# ── Flag helpers ───────────────────────────────────────────────────────────────

def _write_flag(path: Path, content: str = "") -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content or datetime.datetime.utcnow().isoformat(), encoding="utf-8")
    except Exception:
        pass


def _read_flag_ttl(path: Path, default: int) -> int:
    """Read TTL stored inside the flag file as 'TIMESTAMP|SECONDS'."""
    try:
        content = path.read_text(encoding="utf-8").strip()
        if "|" in content:
            return int(content.split("|", 1)[1])
    except Exception:
        pass
    return default


def _flag_active(path: Path, ttl: int) -> bool:
    try:
        if not path.exists():
            return False
        if time.time() - path.stat().st_mtime > ttl:
            path.unlink(missing_ok=True)
            return False
        return True
    except Exception:
        return False


def _daily_flag_active() -> bool:
    ttl = _read_flag_ttl(_DAILY_FLAG, _DEFAULT_DAILY_TTL)
    return _flag_active(_DAILY_FLAG, ttl)


def _verify_cli_health() -> bool:
    """
    Ping the CLI worker /health endpoint to get ground truth on claude_available.
    Returns True if the live endpoint confirms Claude CLI is up.
    Never raises — returns False on any network error.
    """
    import json as _json
    cli_url = _cli_worker_url()
    if not cli_url:
        return False
    try:
        with urllib.request.urlopen(f"{cli_url}/health", timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            return bool(data.get("claude_available", False))
    except Exception:
        return False


def is_pro_available() -> bool:
    """
    True if Pro subscription can be used right now.

    If the CLI_DOWN flag is set, we verify against the LIVE /health endpoint
    before trusting it — stale flags are auto-cleared when the CLI is actually up.
    This prevents false negatives that push traffic to the Anthropic API
    unnecessarily (and burn credits).
    """
    if _daily_flag_active() or _flag_active(_BURST_FLAG, _BURST_TTL):
        return False  # Daily/burst limits are legitimate — no override needed

    if _flag_active(_CLI_DOWN_FLAG, _CLI_DOWN_TTL):
        # Flag says CLI is down — but verify against live /health before trusting it
        if _verify_cli_health():
            # Health endpoint says CLI is actually UP — flag is stale, clear it
            _log("CLI_DOWN flag was set but /health reports claude_available=true — clearing stale flag.")
            clear_cli_down_flag()
            return True
        return False  # Health endpoint confirmed CLI is genuinely down

    return True


def should_attempt_cli() -> bool:
    """
    True if ANY Claude CLI path should be attempted (inspiring-cat or local).

    Unlike is_pro_available(), BURST-only situations still return True —
    super-agent's local subprocess fallback works even when inspiring-cat is in
    its 30-min cooldown. DAILY quota and CLI_DOWN flags are still hard blocks.
    """
    if _flag_active(_BURST_FLAG, _BURST_TTL):
        return True  # BURST: skip inspiring-cat but still try local CLI
    return is_pro_available()


def is_cli_down() -> bool:
    """
    True if Claude CLI is genuinely down — verifies against live /health endpoint
    rather than trusting a potentially stale flag file.
    """
    if not _flag_active(_CLI_DOWN_FLAG, _CLI_DOWN_TTL):
        return False  # No flag set — CLI assumed available
    # Flag is set — cross-check with live endpoint
    if _verify_cli_health():
        _log("is_cli_down: CLI_DOWN flag stale — /health says up. Clearing flag.")
        clear_cli_down_flag()
        return False
    return True


def clear_cli_down_flag() -> None:
    """Clear the CLI_DOWN flag and restore counter — called by watchdog when CLI recovers."""
    for f in (_CLI_DOWN_FLAG, _RESTORE_COUNT_FLAG):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def reset_pro_flag() -> None:
    """Clear all flags — Pro becomes primary immediately."""
    for f in (_DAILY_FLAG, _BURST_FLAG, _CLI_DOWN_FLAG, _RESTORE_COUNT_FLAG):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def verify_pro_auth() -> dict:
    """
    Run `claude auth status` and return the ACTUAL verified auth state.

    Syncs CLI_DOWN flag based on real result so the system never reports
    stale/assumed status.  Called by the health check (every 30 min) and
    the /credits/pro-status endpoint.

    Returns:
        verified     — True if the check completed (regardless of result)
        pro_valid    — True only if authMethod=claude.ai
        logged_in    — True if any auth is present
        auth_method  — "claude.ai" | "apiKey" | ""
        subscription — e.g. "pro" | ""
        message      — human-readable summary of what was found
    Never raises.
    """
    import json as _json
    import re as _re

    # ── Try CLI worker health endpoint first ──────────────────────────────────
    cli_url = _cli_worker_url()
    if cli_url:
        try:
            with urllib.request.urlopen(f"{cli_url}/health", timeout=10) as resp:
                health = _json.loads(resp.read().decode("utf-8"))
                claude_ok = health.get("claude_available", False)
                if not claude_ok:
                    _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
                    msg = "CLI worker reports claude unavailable — CLI_DOWN flag set."
                    _log(f"verify_pro_auth: {msg}")
                    return {"verified": True, "pro_valid": False, "logged_in": False,
                            "auth_method": "", "subscription": "", "message": msg}
                # Binary is up — do a full auth check via task
                raw = _submit_and_poll("claude_auth", {}, timeout=20) or ""
                # Fall through to parse raw below
        except Exception:
            raw = ""
        if raw:
            # Parse and return — same logic as subprocess path
            try:
                data = _json.loads(raw)
            except Exception:
                data = {}
                if '"loggedIn":true' in raw or '"loggedIn": true' in raw:
                    data["loggedIn"] = True
                m = re.search(r'"authMethod"\s*:\s*"([^"]*)"', raw)
                if m:
                    data["authMethod"] = m.group(1)
                m = re.search(r'"subscriptionType"\s*:\s*"([^"]*)"', raw)
                if m:
                    data["subscriptionType"] = m.group(1)

            logged_in    = bool(data.get("loggedIn", False))
            auth_method  = data.get("authMethod", "")
            subscription = data.get("subscriptionType", "")
            is_pro       = logged_in and auth_method == "claude.ai"

            if is_pro:
                clear_cli_down_flag()
                msg = f"Claude Pro auth VERIFIED ✓ (via CLI worker) — authMethod=claude.ai subscription={subscription or 'unknown'}"
                _log(f"verify_pro_auth: {msg}")
                return {"verified": True, "pro_valid": True, "logged_in": True,
                        "auth_method": auth_method, "subscription": subscription, "message": msg}
            else:
                msg = f"Logged in but not Pro (authMethod={auth_method or 'unknown'}) via CLI worker"
                return {"verified": True, "pro_valid": False, "logged_in": logged_in,
                        "auth_method": auth_method, "subscription": subscription, "message": msg}

    # ── Direct subprocess fallback ────────────────────────────────────────────
    try:
        _nokey_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        _nokey_env["HOME"] = "/root"
        proc = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=15,
            env=_nokey_env,
        )
        raw = (proc.stdout or proc.stderr or "").strip()

        # Parse JSON output (claude auth status outputs JSON)
        try:
            data = _json.loads(raw)
        except Exception:
            # Fallback: regex scan for key fields in the raw text
            data = {}
            if '"loggedIn":true' in raw or '"loggedIn": true' in raw:
                data["loggedIn"] = True
            m = _re.search(r'"authMethod"\s*:\s*"([^"]*)"', raw)
            if m:
                data["authMethod"] = m.group(1)
            m = _re.search(r'"subscriptionType"\s*:\s*"([^"]*)"', raw)
            if m:
                data["subscriptionType"] = m.group(1)

        logged_in   = bool(data.get("loggedIn", False))
        auth_method = data.get("authMethod", "")
        subscription = data.get("subscriptionType", "")
        is_pro      = logged_in and auth_method == "claude.ai"

        if is_pro:
            # Verified valid — clear CLI_DOWN if it was stale
            clear_cli_down_flag()
            msg = f"Claude Pro auth VERIFIED ✓ — authMethod=claude.ai subscription={subscription or 'unknown'}"
            _log(f"verify_pro_auth: {msg}")
            return {
                "verified": True, "pro_valid": True,
                "logged_in": True, "auth_method": auth_method,
                "subscription": subscription, "message": msg,
            }
        elif logged_in:
            msg = f"Logged in but not Pro (authMethod={auth_method or 'unknown'}) — API key mode only"
            _log(f"verify_pro_auth: {msg}")
            return {
                "verified": True, "pro_valid": False,
                "logged_in": True, "auth_method": auth_method,
                "subscription": subscription, "message": msg,
            }
        else:
            # Not authenticated — set CLI_DOWN so we stop wasting subprocess calls
            _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
            msg = "Not authenticated — CLI_DOWN flag set. Run 'claude login' in VS Code terminal to restore Pro."
            _log(f"verify_pro_auth: {msg}")
            return {
                "verified": True, "pro_valid": False,
                "logged_in": False, "auth_method": "",
                "subscription": "", "message": msg,
            }

    except FileNotFoundError:
        _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
        msg = "claude CLI binary not found — CLI_DOWN flag set."
        _log(f"verify_pro_auth: {msg}")
        return {"verified": True, "pro_valid": False, "logged_in": False, "auth_method": "", "subscription": "", "message": msg}
    except subprocess.TimeoutExpired:
        msg = "claude auth status timed out (15s) — status unconfirmed, no flag changed."
        _log(f"verify_pro_auth: {msg}")
        return {"verified": False, "pro_valid": None, "logged_in": None, "auth_method": "", "subscription": "", "message": msg}
    except Exception as e:
        return {"verified": False, "pro_valid": None, "logged_in": None, "auth_method": "", "subscription": "", "message": f"Auth check error: {e}"}


def _log(msg: str) -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source="pro_router")
    except Exception:
        pass


# ── Credential auto-restore ───────────────────────────────────────────────────

def _try_restore_claude_auth() -> bool:
    """
    Attempt to restore Claude CLI credentials.

    Source priority (freshest first):
      1. /workspace/.claude_credentials_backup.json  — written by Playwright auto-login
         on inspiring-cat; shared via Railway volume; may be NEWER than the env var
         that was baked in at container start.
      2. CLAUDE_SESSION_TOKEN env var (base64) — set at deploy time.

    Called automatically when an auth error is detected mid-session.
    Writes to all known credential paths so different CLI versions all find
    the token regardless of which file they look for.

    Returns True if credentials were restored AND `claude auth status` confirms
    that the session is now valid.
    """
    import base64 as _b64

    # ── Source 1: volume backup (may be fresher than env var) ────────────────
    _volume_backup = Path("/workspace/.claude_credentials_backup.json")
    decoded: bytes | None = None

    if _volume_backup.exists():
        try:
            _vol_bytes = _volume_backup.read_bytes()
            # Sanity-check: must look like a JSON credentials file
            if _vol_bytes.startswith(b"{") and b"token" in _vol_bytes.lower():
                decoded = _vol_bytes
                _log("Auto-restore: using volume backup credentials (written by latest Playwright login).")
        except Exception as _ve:
            _log(f"Auto-restore: volume backup unreadable — {_ve}")

    # ── Source 2: env var fallback ────────────────────────────────────────────
    if decoded is None:
        token = os.environ.get("CLAUDE_SESSION_TOKEN", "")
        if not token:
            _log("Auto-restore skipped — CLAUDE_SESSION_TOKEN env var not set and no volume backup.")
            return False
        try:
            decoded = _b64.b64decode(token + "==")
            _log("Auto-restore: using CLAUDE_SESSION_TOKEN env var (volume backup absent or invalid).")
        except Exception as e:
            _log(f"Auto-restore failed: cannot base64-decode CLAUDE_SESSION_TOKEN — {e}")
            return False

    # Write to EVERY location Claude Code CLI may look for credentials
    cred_dir = Path("/root/.claude")
    cred_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for fpath in [
        cred_dir / ".credentials.json",   # entrypoint.sh default
        cred_dir / "credentials.json",    # alternative path (no dot prefix)
        Path("/root/.claude.json"),        # Claude Code CLI < 1.x global config
    ]:
        try:
            fpath.write_bytes(decoded)
            fpath.chmod(0o600)
            written += 1
        except Exception:
            pass

    if written == 0:
        _log("Auto-restore failed: could not write any credential file.")
        return False

    # Verify the restored token with a tight timeout
    try:
        _nokey = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        _nokey["HOME"] = "/root"
        r = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=12,
            env=_nokey,
        )
        out = r.stdout + r.stderr
        # Require authMethod=claude.ai specifically — "loggedIn":true alone
        # also matches API-key auth which won't give Pro CLI access.
        _out_compact = out.replace(": ", ":")
        if '"authMethod":"claude.ai"' in _out_compact:
            _log(f"Auto-restore VERIFIED ✓ — credentials written to {written} paths.")
            return True
        # loggedIn but wrong authMethod means token is for API key, not Pro OAuth
        _log(f"Auto-restore: token is NOT Pro OAuth (authMethod≠claude.ai): {out[:200]!r}")
        return False
    except subprocess.TimeoutExpired:
        _log("Auto-restore: auth verify timed out — token may be expired.")
        return False
    except Exception as e:
        _log(f"Auto-restore verify exception: {e}")
        return False


def _pre_flight_auth_ok() -> bool:
    """
    Quick 12-second auth check before a full `claude -p` subprocess call.

    Prevents a 360-second hang when the container restarted and the credentials
    file was wiped — the auth check fails in 12s instead of 360s.

    Only used on the direct subprocess path (when CLI_WORKER_URL is not set).
    The CLI worker path has its own health-check via _verify_cli_health().

    Returns True if auth looks fine and the full prompt call can proceed.
    """
    try:
        _env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        _env["HOME"] = "/root"
        r = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=12,
            env=_env,
        )
        out = r.stdout + r.stderr
        # Happy path — handle both compact and pretty-printed JSON
        _out_compact = out.replace(": ", ":")
        if '"authMethod":"claude.ai"' in _out_compact or '"loggedIn":true' in _out_compact:
            return True
        # Auth failure detected — try to self-restore before giving up
        if any(p in out.lower() for p in _CLI_DOWN_PHRASES):
            _log("Pre-flight: auth failure detected — attempting auto-restore…")
            if _try_restore_claude_auth():
                _log("Pre-flight: auth restored ✓ — proceeding with prompt call.")
                return True
            # Restore failed — set CLI_DOWN and bail fast
            _write_flag(_CLI_DOWN_FLAG,
                        f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
            _log("Pre-flight: auth restore failed — CLI_DOWN set (10 min).")
            return False
        # Unknown output — optimistically continue (avoids false negatives)
        return True
    except FileNotFoundError:
        _write_flag(_CLI_DOWN_FLAG,
                    f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
        return False
    except subprocess.TimeoutExpired:
        _log("Pre-flight auth check timed out (12s) — skipping CLI.")
        return False
    except Exception:
        return True  # Unknown error — don't block, let the full call decide


# ── Timeout self-healing ──────────────────────────────────────────────────────

def _fire_timeout_investigation(prompt_len: int, proc=None) -> None:
    """
    Spawn a daemon thread to investigate a CLI timeout in parallel with the
    main response falling back to Gemini/API.  Never blocks the caller.

    Actions taken autonomously:
    1. Kill the hung process (if one is provided)
    2. Ping CLI worker /health for ground-truth status
    3. Kill any orphaned 'claude' processes left in the container
    4. Log findings to the activity log (visible in status bar)
    5. Record the timeout in a simple counter file for frequency tracking
    6. If CLI worker is unhealthy → set CLI_DOWN flag so future calls skip CLI
       and route to Gemini instead of timing out again
    """
    import threading

    def _investigate():
        try:
            # 1. Kill the specific timed-out process
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

            # 2. Kill any other orphaned claude processes in the container
            try:
                import subprocess as _sp
                _sp.run(["pkill", "-f", "claude -p"], timeout=5,
                        capture_output=True)
            except Exception:
                pass

            # 3. Ping CLI worker for ground truth
            healthy = _verify_cli_health()
            health_note = "CLI worker healthy ✓" if healthy else "CLI worker UNHEALTHY ✗"

            # 4. If worker is unhealthy, set CLI_DOWN so retries skip CLI
            if not healthy:
                _write_flag(_CLI_DOWN_FLAG,
                            f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
                health_note += " → CLI_DOWN flag set (10 min)"

            # 5. Record timeout in counter file
            _timeout_count = 0
            _counter_file = _FLAG_DIR / ".cli_timeout_count"
            try:
                if _counter_file.exists():
                    _timeout_count = int(_counter_file.read_text().strip() or "0")
                _timeout_count += 1
                _counter_file.write_text(str(_timeout_count))
            except Exception:
                pass

            _log(
                f"[AUTO-HEAL] CLI timeout on prompt_len={prompt_len} chars | "
                f"total_timeouts={_timeout_count} | {health_note}"
            )

            # 6. Alert if timeouts are becoming frequent (>5 in a session)
            if _timeout_count >= 5:
                _log(
                    "[AUTO-HEAL] ⚠️ CLI timeouts are frequent — consider increasing "
                    "CLI_WORKER_URL capacity or checking inspiring-cat CPU/memory."
                )

        except Exception as _e:
            _log(f"[AUTO-HEAL] Timeout investigation failed: {_e}")

    threading.Thread(target=_investigate, daemon=True).start()


# ── Classifier ─────────────────────────────────────────────────────────────────

def _classify_and_set_flag(stdout: str, stderr: str) -> None:
    """Classify CLI error output and set the appropriate temporary flag."""
    combined = f"{stdout} {stderr}".lower()

    # Ignore weekly/billing/upgrade messages — don't set any flag
    if any(p in combined for p in _IGNORED_PHRASES):
        _log(
            "Pro CLI returned a weekly/billing message — intentionally ignored. "
            "Pro subscription continues at 100% weekly usage. No API fallback set."
        )
        return

    # CLI DOWN: auth failure / token expired
    if any(p in combined for p in _CLI_DOWN_PHRASES):
        _log("Pro CLI AUTH FAILURE detected — attempting autonomous credential restore…")

        # Read current restore attempt count
        try:
            _restore_count = int(_RESTORE_COUNT_FLAG.read_text().strip())
        except Exception:
            _restore_count = 0

        # If we've already restored N times but CLI keeps failing, the stored token
        # is expired — escalate immediately to Playwright full recovery chain.
        if _restore_count >= _RESTORE_ATTEMPT_LIMIT:
            _log(
                f"Pro CLI AUTH FAILURE — restore attempted {_restore_count}x but token "
                "is still expired. Escalating to Playwright auto-login via n8n email monitor…"
            )
            _RESTORE_COUNT_FLAG.unlink(missing_ok=True)
            try:
                import threading as _threading
                from .cli_auto_login import full_recovery_chain
                def _bg_recover():
                    ok = full_recovery_chain()
                    if ok:
                        _log("full_recovery_chain: Playwright login SUCCESS ✓ — CLI restored.")
                        clear_cli_down_flag()
                    else:
                        _log("full_recovery_chain: Playwright login FAILED — manual login required.")
                _threading.Thread(target=_bg_recover, daemon=True).start()
                _log("Playwright auto-login started in background thread.")
            except Exception as _re:
                _log(f"Could not start full_recovery_chain: {_re}")
            # Set CLI_DOWN during recovery (watchdog will clear when done)
            _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
            _queue_progress("🔄 Token expired — auto-login via email verification in progress…")
            return

        # Before flagging CLI as down: try to re-write credentials from env var.
        # This handles the common case where the container restarted and wiped
        # /root/.claude/ from the ephemeral filesystem.
        if _try_restore_claude_auth():
            # Credentials restored successfully — increment counter so we know
            # if the NEXT call also fails (meaning the token itself is expired)
            try:
                _RESTORE_COUNT_FLAG.write_text(str(_restore_count + 1))
            except Exception:
                pass
            _log(
                f"Pro CLI auth SELF-HEALED ✓ — credentials restored from "
                f"CLAUDE_SESSION_TOKEN env var (attempt {_restore_count + 1}/{_RESTORE_ATTEMPT_LIMIT}). "
                "CLI_DOWN flag NOT set. Next call will retry Claude CLI normally."
            )
            _queue_progress(
                "🔑 Auth restored from env var — Claude CLI is back (self-heal ✓)"
            )
            return  # Do not set CLI_DOWN — retry next call

        # Auto-restore failed (token expired or env var not set) — escalate immediately
        _RESTORE_COUNT_FLAG.unlink(missing_ok=True)
        _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
        _log(
            "Pro CLI AUTH FAILURE — token expired or not logged in. "
            "Auto-restore attempted but failed. "
            "Falling back to Gemini → Anthropic API. "
            "To fix: VS Code terminal → 'claude login' → copy new token → "
            "update CLAUDE_SESSION_TOKEN in Railway Variables."
        )
        _queue_progress(
            "⚠️ Claude CLI session expired — fell back to Gemini/API. "
            "Update CLAUDE_SESSION_TOKEN in Railway Variables to restore."
        )
        try:
            from ..alerts.notifier import alert_claude_cli_down
            alert_claude_cli_down(reason="Auth failure / token expired — auto-restore also failed")
        except Exception:
            pass
        return

    # DAILY limit — parse reset time from message, set TTL-aware flag
    if any(p in combined for p in _DAILY_PHRASES):
        ttl   = _parse_reset_seconds(combined)
        hours = round(ttl / 3600, 1)
        _write_flag(_DAILY_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{ttl}")
        _log(
            f"Pro DAILY LIMIT hit — using ANTHROPIC_API_KEY for ~{hours}h. "
            "Weekly quota NOT exhausted. Pro resumes automatically."
        )
        try:
            from ..alerts.notifier import alert_claude_daily_limit
            alert_claude_daily_limit(reset_hours=hours)
        except Exception:
            pass
        return

    # BURST throttle — momentary overload, 30-min backoff
    if any(p in combined for p in _BURST_PHRASES):
        _write_flag(_BURST_FLAG)
        _log(
            "Pro BURST THROTTLE — momentary overload, backing off 30 min. "
            "No quota consumed. Pro resumes automatically."
        )
        return

    # Unrecognised error — log only, no flag, Pro retried next call
    _log(
        f"Pro CLI unrecognised error (no flag set) — "
        f"stdout: {stdout[:200]!r}  stderr: {stderr[:200]!r}"
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def _track_cli(state: str, task: str = "") -> None:
    """Update Claude CLI Pro status in the dashboard tracker."""
    try:
        from .agent_status_tracker import mark_working, mark_done, mark_sick
        if state == "working":
            mark_working("Claude CLI Pro", task)
        elif state == "done":
            mark_done("Claude CLI Pro")
        elif state == "sick":
            mark_sick("Claude CLI Pro")
    except Exception:
        pass


def try_pro(prompt: str, system: str = "") -> str | None:
    """
    Attempt to answer via Claude Code CLI (Pro subscription).

    Timeout strategy: dynamic — scales with prompt length so large prompts
    (e.g. complex n8n workflow design) are never cut short prematurely.
      < 1 500 chars  → 120 s
      1 500 – 4 000  → 200 s
      > 4 000 chars  → 360 s  (6 minutes)

    On timeout:
    - Kill the hung process immediately
    - Fire a parallel daemon thread for autonomous investigation
      (health ping, orphan kill, counter, flag-set if worker unhealthy)
    - Retry ONCE with the full dynamic timeout (catches transient hangs)

    Returns:
        str   — CLI response. Use directly.
        None  — unavailable or timed out; caller falls back to Gemini/API.
    """
    # BURST flag only disables inspiring-cat — the local subprocess fallback in
    # super-agent still works (separate container, separate credentials).
    # Check BURST separately so we skip inspiring-cat but still try local CLI.
    _burst_active = _flag_active(_BURST_FLAG, _BURST_TTL)
    if not _burst_active and not is_pro_available():
        _track_cli("sick")  # Dashboard: CLI is down/daily limit
        return None  # DAILY or CLI_DOWN — full skip (inspiring-cat AND local)

    full_prompt = f"{system}\n\n{prompt}" if system and system.strip() else prompt
    _timeout = _dynamic_timeout(full_prompt)
    _track_cli("working", prompt[:100])

    # ── Try via CLI worker (skipped when BURST active — saves 15-50s per call) ─
    cli_result = None
    if not _burst_active:
        cli_result = _submit_and_poll("claude_pro", {"prompt": full_prompt},
                                      timeout=_timeout + 30)  # +30 s polling buffer
        if cli_result is not None:
            _cli_lower = cli_result.lower()
            if any(p in _cli_lower for p in _DAILY_PHRASES):
                _classify_and_set_flag(cli_result, "")
                _track_cli("sick")  # Daily limit hit
                return None  # Daily quota same account — local subprocess won't help
            elif any(p in _cli_lower for p in _BURST_PHRASES + _CLI_DOWN_PHRASES):
                _classify_and_set_flag(cli_result, "")
                _queue_progress("⚠️ inspiring-cat throttled — trying super-agent local CLI…")
                # Fall through to local subprocess — separate credentials, may work
            # Credit/billing errors — CLI worker's OAuth session is expired and it
            # fell back to an API key with no credits. Set BURST flag (30 min) so
            # all subsequent requests skip the CLI worker immediately instead of
            # waiting 15-50s for it to fail again on each message.
            elif any(p in _cli_lower for p in _CREDIT_ERROR_PHRASES + _IGNORED_PHRASES):
                _log(
                    f"CLI worker returned a credit/billing error — setting BURST flag (30 min) "
                    f"and falling through to super-agent local CLI. Response: {cli_result[:120]!r}"
                )
                _write_flag(_BURST_FLAG)
                _queue_progress("⚠️ inspiring-cat CLI expired — trying super-agent local CLI…")
                # Do NOT return here — fall through to the direct subprocess below.
            elif any(p in _cli_lower for p in _MCP_PERMISSION_PHRASES):
                # CLI worker asked for MCP tool permission — resubmit with auto-accept input.
                # This happens when settings.json hasn't taken effect. Retry via direct subprocess
                # with stdin piped so we can auto-answer any permission prompts with "1" (Allow once).
                _log(
                    "CLI worker returned MCP permission request — retrying via direct subprocess "
                    "with auto-accept stdin to approve MCP tools without user intervention."
                )
                _queue_progress("🔑 Auto-accepting MCP tool permissions…")
                # Fall through to direct subprocess below (which uses stdin=PIPE with auto-accept)
            else:
                _track_cli("done")  # Clean response
                _RESTORE_COUNT_FLAG.unlink(missing_ok=True)  # success — reset restore counter
                return cli_result or None  # Clean response — use it
    else:
        _queue_progress("⚠️ inspiring-cat in cooldown — routing to local Claude CLI…")

    # ── Direct subprocess fallback (CLI worker unreachable — use local CLI) ─────
    # This path activates when inspiring-cat is down/unreachable or in BURST
    # cooldown. super-agent has its own Claude CLI credentials (Railway volume at
    # /root/.claude/) so it can handle the request without Gemini or the API.
    _queue_progress("⚡ Trying super-agent local CLI…")
    # Pre-flight auth check — fails in 12s if session is gone (e.g. container
    # restarted and /root/.claude/ was wiped), preventing a 360s hang.
    # Also attempts autonomous credential restore before giving up.
    if not _pre_flight_auth_ok():
        _track_cli("sick")  # Auth failed
        return None

    def _run_subprocess(t: int):
        # Unset ANTHROPIC_API_KEY so claude CLI uses OAuth (claude.ai Pro)
        # instead of the API key. When API key is present in env, claude -p
        # prefers it over OAuth — causing "Credit balance is too low" even
        # when Pro credentials are valid.
        _env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        _env["HOME"] = "/root"
        return subprocess.Popen(
            ["claude", "-p", full_prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,  # pipe so we can auto-accept MCP permission prompts
            text=True,
            cwd="/workspace",
            env=_env,
        )

    # Pre-supply "1\n" * 30 as stdin input so any interactive MCP permission
    # prompts (e.g. "Allow mcp__...? 1) Allow once  2) Allow always  3) Deny")
    # are automatically answered with option 1 (Allow once).
    _AUTO_ACCEPT = "1\n" * 30

    proc = None
    try:
        proc = _run_subprocess(_timeout)
        stdout, stderr = proc.communicate(input=_AUTO_ACCEPT, timeout=_timeout)
        stdout = (stdout or "").strip()
        stderr = (stderr or "").strip()

        if proc.returncode == 0 and stdout:
            # If MCP permission phrases still appear in stdout despite auto-accept stdin,
            # it means the permission model is different — extract any actual content after
            # the permission lines, or fall through to Gemini/API as last resort.
            if any(p in stdout.lower() for p in _MCP_PERMISSION_PHRASES):
                # Try to find actual response content after the permission lines
                lines = stdout.splitlines()
                content_lines = [l for l in lines if not any(p in l.lower() for p in _MCP_PERMISSION_PHRASES)]
                clean = "\n".join(content_lines).strip()
                if clean and len(clean) > 20:
                    _log("Local CLI: extracted content past MCP permission lines — using it.")
                    _track_cli("done")
                    return clean
                _log("Local CLI returned only MCP permission request — falling through to Gemini/API.")
                _track_cli("sick")
                return None
            # Check for MCP tool execution errors — CLI succeeded but output describes a failure
            if any(p in stdout.lower() for p in _MCP_TOOL_ERROR_PHRASES):
                _log(f"Local CLI: MCP tool error detected — falling through. Snippet: {stdout[:150]!r}")
                _track_cli("sick")
                return None
            _track_cli("done")
            return stdout

        if stdout or stderr:
            # Don't misclassify valid auth JSON as a failure — if the output
            # contains a valid auth status (loggedIn + authMethod=claude.ai),
            # it's a transient CLI error, not an auth failure.
            combined = f"{stdout} {stderr}"
            _combined_compact = combined.replace(": ", ":")
            if '"authMethod":"claude.ai"' in _combined_compact and '"loggedIn":true' in _combined_compact:
                _log("CLI returned non-zero exit but auth is valid — transient error, not flagging as down")
            else:
                _classify_and_set_flag(stdout, stderr)

        _track_cli("sick")  # Non-zero exit / error output
        return None

    except FileNotFoundError:
        _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
        _log(
            "Pro CLI binary not found — setting CLI_DOWN flag (10 min). "
            "Switching to Gemini/API. Watchdog will auto-revert when CLI is available."
        )
        _track_cli("sick")
        return None

    except subprocess.TimeoutExpired:
        _log(
            f"CLI TIMEOUT (attempt 1) — prompt_len={len(full_prompt)} chars, "
            f"timeout={_timeout}s. Firing parallel investigation + retrying once."
        )
        # Queue visible self-healing milestone for the current SSE stream
        _queue_progress(
            f"🔧 CLI timed out after {_timeout}s — "
            f"Super Agent is self-healing: killing hung process, retrying with fresh connection…"
        )
        # Fire parallel self-healing in a daemon thread (non-blocking)
        _fire_timeout_investigation(len(full_prompt), proc=proc)

        # ── Retry once with the full timeout ─────────────────────────────────
        # The investigation killed any hung process; a fresh Popen has a clean slate.
        proc2 = None
        try:
            _queue_progress("🔄 Self-heal retry in progress — waiting for Claude CLI response…")
            proc2 = _run_subprocess(_timeout)
            stdout2, stderr2 = proc2.communicate(input=_AUTO_ACCEPT, timeout=_timeout)
            stdout2 = (stdout2 or "").strip()
            stderr2 = (stderr2 or "").strip()
            if proc2.returncode == 0 and stdout2:
                _log("CLI TIMEOUT RETRY succeeded ✓")
                _queue_progress("✅ Self-heal successful — CLI responded on retry, no API credits used")
                return stdout2
            if stdout2 or stderr2:
                _classify_and_set_flag(stdout2, stderr2)
        except subprocess.TimeoutExpired:
            if proc2:
                try:
                    proc2.kill()
                except Exception:
                    pass
            _log(
                f"CLI TIMEOUT (attempt 2 also timed out) — "
                f"prompt_len={len(full_prompt)} chars. Falling back to Gemini/API."
            )
            _queue_progress(
                "⚠️ Both CLI attempts timed out — self-healing routed to Gemini/API "
                "(background investigation continues to restore CLI)"
            )
        except Exception:
            pass

        return None

    except Exception:
        return None


# ── Status reporting ───────────────────────────────────────────────────────────

def get_status() -> dict:
    daily_active    = _daily_flag_active()
    burst_active    = _flag_active(_BURST_FLAG, _BURST_TTL)
    cli_down_active = _flag_active(_CLI_DOWN_FLAG, _CLI_DOWN_TTL)

    def _remaining_h(path: Path, ttl: int) -> float | None:
        try:
            if not path.exists():
                return None
            age = time.time() - path.stat().st_mtime
            return max(0.0, round((ttl - age) / 3600, 1))
        except Exception:
            return None

    def _remaining_min(path: Path, ttl: int) -> int | None:
        h = _remaining_h(path, ttl)
        return round(h * 60) if h is not None else None

    daily_ttl         = _read_flag_ttl(_DAILY_FLAG, _DEFAULT_DAILY_TTL)
    cli_down_ttl      = _read_flag_ttl(_CLI_DOWN_FLAG, _CLI_DOWN_TTL)
    daily_resets_h    = _remaining_h(_DAILY_FLAG, daily_ttl)
    burst_resets_min  = _remaining_min(_BURST_FLAG, _BURST_TTL)
    cli_down_resets_m = _remaining_min(_CLI_DOWN_FLAG, cli_down_ttl)

    if cli_down_active:
        mode    = "api_fallback_cli_down"
        message = (
            f"Pro CLI unavailable (auth failure or binary missing) — "
            f"using ANTHROPIC_API_KEY. "
            f"Watchdog probing every 5 min, auto-reverts when CLI recovers "
            f"(~{cli_down_resets_m}min until next probe window expires)."
        )
    elif daily_active:
        mode    = "api_fallback_daily"
        message = (
            f"Daily Pro limit hit — using ANTHROPIC_API_KEY for ~{daily_resets_h}h. "
            "Weekly quota is NOT exhausted. Pro resumes automatically."
        )
    elif burst_active:
        mode    = "api_fallback_burst"
        message = f"Pro momentarily throttled — using API for ~{burst_resets_min}min. No quota consumed."
    else:
        mode    = "pro_primary"
        message = "Pro subscription active (100% weekly usage). ANTHROPIC_API_KEY reserved as fallback only."

    return {
        "mode": mode,
        "pro_available": not daily_active and not burst_active and not cli_down_active,
        "flags": {
            "daily_limit_active": daily_active,
            "burst_throttled":    burst_active,
            "cli_down":           cli_down_active,
        },
        "resets_in": {
            "daily_hours":      daily_resets_h,
            "burst_minutes":    burst_resets_min,
            "cli_down_minutes": cli_down_resets_m,
        },
        "message": message,
        "policy": "Weekly limits ignored — Pro used at 100% capacity. API key only activates on daily cap, burst throttle, or CLI unavailability.",
    }
