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
import os
import re
import time
import subprocess
import datetime
from pathlib import Path

_FLAG_DIR      = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
_DAILY_FLAG    = _FLAG_DIR / ".pro_daily"
_BURST_FLAG    = _FLAG_DIR / ".pro_burst"
_CLI_DOWN_FLAG = _FLAG_DIR / ".pro_cli_down"

_DEFAULT_DAILY_TTL = 4 * 3600   # fallback if reset time can't be parsed from message
_BURST_TTL         = 30 * 60    # 30 minutes
_CLI_DOWN_TTL      = 10 * 60    # 10 minutes — watchdog will clear sooner if CLI recovers
_RESET_BUFFER      = 15 * 60    # 15-min buffer added to parsed reset time

_TIMEOUT = 120  # seconds per CLI subprocess call

# ── Phrase banks ───────────────────────────────────────────────────────────────

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
    "unauthorized",
    "forbidden",
    "auth failed",
    "credentials",
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


def is_pro_available() -> bool:
    """True if Pro subscription can be used right now."""
    return (
        not _daily_flag_active()
        and not _flag_active(_BURST_FLAG, _BURST_TTL)
        and not _flag_active(_CLI_DOWN_FLAG, _CLI_DOWN_TTL)
    )


def is_cli_down() -> bool:
    """True if the CLI_DOWN flag is active (auth failure, binary missing, etc.)."""
    return _flag_active(_CLI_DOWN_FLAG, _CLI_DOWN_TTL)


def clear_cli_down_flag() -> None:
    """Clear the CLI_DOWN flag — called by watchdog when CLI recovers."""
    try:
        _CLI_DOWN_FLAG.unlink(missing_ok=True)
    except Exception:
        pass


def reset_pro_flag() -> None:
    """Clear all flags — Pro becomes primary immediately."""
    for f in (_DAILY_FLAG, _BURST_FLAG, _CLI_DOWN_FLAG):
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

    try:
        proc = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "HOME": "/root"},
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

    # CLI DOWN: auth failure / token expired — set CLI_DOWN flag, watchdog will recover
    if any(p in combined for p in _CLI_DOWN_PHRASES):
        _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
        _log(
            "Pro CLI AUTH FAILURE — token expired or not logged in. "
            "Switching to ANTHROPIC_API_KEY. "
            "Watchdog probing every 5 min — will auto-revert to Pro when CLI recovers. "
            "To fix manually: VS Code terminal → 'claude login' → update CLAUDE_SESSION_TOKEN."
        )
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

def try_pro(prompt: str, system: str = "") -> str | None:
    """
    Attempt to answer via Claude Code CLI (Pro subscription).

    Returns:
        str   — CLI response. Use directly.
        None  — daily limit or burst active; caller must use ANTHROPIC_API_KEY.
    """
    if not is_pro_available():
        return None

    full_prompt = f"{system}\n\n{prompt}" if system and system.strip() else prompt

    try:
        proc = subprocess.run(
            ["claude", "-p", full_prompt],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd="/workspace",
            env={**os.environ, "HOME": "/root"},
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode == 0 and stdout:
            return stdout

        if stdout or stderr:
            _classify_and_set_flag(stdout, stderr)

        return None

    except FileNotFoundError:
        # CLI binary gone — set CLI_DOWN so we don't waste time on every call
        _write_flag(_CLI_DOWN_FLAG, f"{datetime.datetime.utcnow().isoformat()}|{_CLI_DOWN_TTL}")
        _log(
            "Pro CLI binary not found — setting CLI_DOWN flag (10 min). "
            "Switching to ANTHROPIC_API_KEY. Watchdog will auto-revert when CLI is available."
        )
        return None
    except subprocess.TimeoutExpired:
        return None              # timed out — API for this call only, no flag
    except Exception:
        return None              # unexpected — silent fallback, no flag


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
