"""
Pro CLI Watchdog — background recovery probe.

When the CLI_DOWN flag is active (auth failure, binary missing, container
crash, token expired), every dispatch call skips the Pro subprocess entirely
and uses ANTHROPIC_API_KEY instead.

This watchdog runs every 5 minutes via APScheduler and probes
`claude --version`.  On success it clears the CLI_DOWN flag so Pro
resumes as primary on the very next request — zero human intervention
required.

Flow:
  CLI fails → pro_router sets .pro_cli_down (10 min TTL)
  Watchdog every 5 min → probe_cli()
      False → log still down, wait another 5 min
      True  → clear flag, log "Pro CLI RECOVERED — reverting to Pro"
  Next dispatch → is_pro_available() True → Pro is primary again

Manual override: POST /credits/reset-pro also clears the flag instantly.
"""
import os
import subprocess

_PROBE_TIMEOUT = 15  # seconds — short so the scheduler thread is never blocked long


def probe_cli() -> bool:
    """
    Check if the Claude CLI is available — via CLI worker /health endpoint
    if CLI_WORKER_URL is set, otherwise via direct subprocess.
    Returns True if CLI is responding.  Never raises.
    """
    import json
    import urllib.request

    cli_url = os.environ.get("CLI_WORKER_URL", "").rstrip("/")
    if cli_url:
        try:
            with urllib.request.urlopen(f"{cli_url}/health", timeout=_PROBE_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return bool(body.get("claude_available", False))
        except Exception:
            return False

    # Fallback: direct subprocess (single-container mode)
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            env={**os.environ, "HOME": "/root"},
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def maybe_recover() -> bool:
    """
    Check if CLI_DOWN flag is active; run a full auth verification; clear flag on recovery.

    Uses verify_pro_auth() (not just --version) so recovery is only declared
    when auth is actually confirmed valid — never assumed from a version ping.

    Called by the APScheduler job every 5 minutes.
    Returns True if recovery was detected (flag cleared).
    Never raises.
    """
    try:
        from .pro_router import is_cli_down, verify_pro_auth
        from ..activity_log import bg_log

        if not is_cli_down():
            return False  # nothing to recover

        # Quick binary check first (cheap) — skip full auth if CLI not even present
        if not probe_cli():
            bg_log(
                "Pro CLI watchdog: CLI binary still unavailable — continuing API fallback. "
                "Will probe again in 5 min.",
                source="pro_cli_watchdog",
            )
            # Proactive escalation: alert if CLI has been down for a long time
            try:
                from ..alerts.notifier import alert_cli_still_down_escalation, _flag_age_minutes
                from .pro_router import _CLI_DOWN_FLAG
                minutes_down = _flag_age_minutes("claude_cli_down")
                # Also check the flag file mtime directly as a backup
                if minutes_down == 0 and _CLI_DOWN_FLAG.exists():
                    import time as _t
                    minutes_down = (_t.time() - _CLI_DOWN_FLAG.stat().st_mtime) / 60
                # Escalate at 30 min, 60 min, 120 min intervals
                if minutes_down >= 30:
                    alert_cli_still_down_escalation(minutes_down)
            except Exception:
                pass
            return False

        # CLI binary is present — now verify actual auth state
        auth = verify_pro_auth()
        if not auth.get("pro_valid"):
            # Auth invalid — attempt credential restore from env var before giving up
            bg_log(
                f"Pro CLI watchdog: auth invalid — attempting credential restore from env var…",
                source="pro_cli_watchdog",
            )
            try:
                from .pro_router import _try_restore_claude_auth
                if _try_restore_claude_auth():
                    auth = verify_pro_auth()
                    if auth.get("pro_valid"):
                        bg_log(
                            "Pro CLI watchdog: credential restore SUCCESS — auth now valid ✓",
                            source="pro_cli_watchdog",
                        )
                        # Fall through to the recovery block below
                    else:
                        bg_log(
                            "Pro CLI watchdog: restore succeeded but auth still invalid. "
                            "CLAUDE_SESSION_TOKEN env var may be stale. Manual login required.",
                            source="pro_cli_watchdog",
                        )
                        return False
                else:
                    bg_log(
                        "Pro CLI watchdog: credential restore failed (no env var or decode error). "
                        "Manual login required.",
                        source="pro_cli_watchdog",
                    )
                    return False
            except Exception as _e:
                bg_log(
                    f"Pro CLI watchdog: restore attempt error — {_e}. Continuing API fallback.",
                    source="pro_cli_watchdog",
                )
                return False

        # verify_pro_auth() already cleared CLI_DOWN flag on success
        bg_log(
            "Pro CLI watchdog: CLI RECOVERED ✓ — auth verified, CLI_DOWN flag cleared. "
            "Reverting to Claude Pro as primary model. ANTHROPIC_API_KEY fallback deactivated.",
            source="pro_cli_watchdog",
        )
        try:
            from ..alerts.notifier import alert_claude_recovered
            subscription = auth.get("subscription", "")
            alert_claude_recovered(subscription=subscription)
        except Exception:
            pass
        return True

    except Exception:
        return False
