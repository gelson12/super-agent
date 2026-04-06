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
    Run `claude --version` to verify the CLI is installed and responding.
    Returns True if exit code is 0.  Never raises.
    """
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
            return False

        # CLI binary is present — now verify actual auth state
        auth = verify_pro_auth()
        if not auth.get("pro_valid"):
            bg_log(
                f"Pro CLI watchdog: CLI binary found but auth still invalid — "
                f"{auth.get('message', '')}. Continuing API fallback.",
                source="pro_cli_watchdog",
            )
            return False

        # verify_pro_auth() already cleared CLI_DOWN flag on success
        bg_log(
            "Pro CLI watchdog: CLI RECOVERED ✓ — auth verified, CLI_DOWN flag cleared. "
            "Reverting to Claude Pro as primary model. ANTHROPIC_API_KEY fallback deactivated.",
            source="pro_cli_watchdog",
        )
        return True

    except Exception:
        return False
