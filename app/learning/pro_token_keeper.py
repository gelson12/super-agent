"""
Pro Token Keeper — keeps Claude.ai Pro OAuth credentials alive indefinitely.

How it works:
  1. Runs every hour (keeps token alive through periodic activity)
  2. Makes a lightweight `claude auth status` call — the CLI auto-refreshes the
     OAuth access token using the refresh token if it's near expiry
  3. Re-encodes the (now refreshed) credentials file as base64
  4. Pushes the updated value back to Railway Variables via the Railway API
     so the NEXT redeploy always gets a fresh token

This creates a self-perpetuating loop:
  Container boot → restore credentials from Railway var
  Daily keeper   → refresh + write updated credentials back to Railway var
  Next boot      → restore freshly-saved credentials → repeat forever

Requirements:
  RAILWAY_TOKEN          — already set (used for autonomous redeploy)
  RAILWAY_PROJECT_ID     — injected automatically by Railway
  RAILWAY_ENVIRONMENT_ID — injected automatically by Railway
  RAILWAY_SERVICE_ID     — injected automatically by Railway

The refresh token inside credentials.json is long-lived (months-years as long
as there is periodic activity). This daily job guarantees activity, so in
practice the credentials never expire.
"""
import base64
import json
import os
import subprocess
import time
from pathlib import Path

_CREDS_FILE = Path("/root/.claude/.credentials.json")
_TIMEOUT = 30


# ── Railway API ────────────────────────────────────────────────────────────────

def _update_railway_variable(name: str, value: str) -> tuple[bool, str]:
    """
    Upsert a Railway service variable via the GraphQL API.
    Returns (success, message).
    """
    token      = os.environ.get("RAILWAY_TOKEN", "")
    project_id = os.environ.get("RAILWAY_PROJECT_ID", "")
    env_id     = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
    service_id = os.environ.get("RAILWAY_SERVICE_ID", "")

    if not all([token, project_id, env_id, service_id]):
        missing = [k for k, v in {
            "RAILWAY_TOKEN": token,
            "RAILWAY_PROJECT_ID": project_id,
            "RAILWAY_ENVIRONMENT_ID": env_id,
            "RAILWAY_SERVICE_ID": service_id,
        }.items() if not v]
        return False, f"Missing Railway env vars: {missing}"

    mutation = """
    mutation VariableUpsert($input: VariableUpsertInput!) {
      variableUpsert(input: $input)
    }
    """
    payload = {
        "query": mutation,
        "variables": {
            "input": {
                "projectId":     project_id,
                "environmentId": env_id,
                "serviceId":     service_id,
                "name":          name,
                "value":         value,
            }
        }
    }

    try:
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "https://backboard.railway.app/graphql/v2",
            data=data,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if "errors" in body:
                return False, f"Railway API error: {body['errors']}"
            return True, "Railway variable updated via API."
    except Exception as e:
        return False, f"Railway API call failed: {e}"


def _update_railway_variable_for_service(name: str, value: str, service_id: str) -> tuple[bool, str]:
    """Push a Railway variable to a SPECIFIC service (e.g. inspiring-cat) by service ID."""
    token      = os.environ.get("RAILWAY_TOKEN", "")
    project_id = os.environ.get("RAILWAY_PROJECT_ID", "")
    env_id     = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

    if not all([token, project_id, env_id, service_id]):
        missing = [k for k, v in {
            "RAILWAY_TOKEN": token, "RAILWAY_PROJECT_ID": project_id,
            "RAILWAY_ENVIRONMENT_ID": env_id, "CLI_WORKER_SERVICE_ID": service_id,
        }.items() if not v]
        return False, f"Missing vars: {missing}"

    mutation = """
    mutation VariableUpsert($input: VariableUpsertInput!) {
      variableUpsert(input: $input)
    }
    """
    payload = {
        "query": mutation,
        "variables": {
            "input": {
                "projectId":     project_id,
                "environmentId": env_id,
                "serviceId":     service_id,
                "name":          name,
                "value":         value,
            }
        }
    }
    try:
        import urllib.request as _ur
        data = json.dumps(payload).encode("utf-8")
        req  = _ur.Request(
            "https://backboard.railway.app/graphql/v2",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if "errors" in body:
                return False, f"Railway API error: {body['errors']}"
            return True, "OK"
    except Exception as e:
        return False, str(e)


def _update_via_cli(name: str, value: str) -> tuple[bool, str]:
    """Fallback: update Railway variable via railway CLI."""
    try:
        result = subprocess.run(
            ["railway", "variables", "--set", f"{name}={value}"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        if result.returncode == 0:
            return True, "Railway variable updated via CLI."
        return False, f"railway CLI failed: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "railway CLI not found."
    except Exception as e:
        return False, f"railway CLI error: {e}"


# ── Token refresh ──────────────────────────────────────────────────────────────

def _ping_cli_to_refresh() -> tuple[bool, str]:
    """
    Call `claude auth status` — the CLI checks the token and uses the refresh
    token to get a new access token if the current one is near expiry,
    writing the updated credentials back to the file automatically.
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=_TIMEOUT,
            env={**os.environ, "HOME": "/root"},
        )
        output = result.stdout.strip()
        if '"loggedIn": true' in output and '"authMethod": "claude.ai"' in output:
            return True, output
        return False, f"Unexpected auth status: {output[:200]}"
    except FileNotFoundError:
        return False, "claude CLI not found."
    except subprocess.TimeoutExpired:
        return False, "claude auth status timed out."
    except Exception as e:
        return False, f"CLI ping error: {e}"


def _read_and_encode_credentials() -> tuple[str | None, str]:
    """Read credentials file and return base64-encoded value."""
    try:
        if not _CREDS_FILE.exists():
            return None, f"Credentials file not found: {_CREDS_FILE}"
        raw = _CREDS_FILE.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return encoded, "OK"
    except Exception as e:
        return None, f"Failed to read credentials: {e}"


# ── Main job ───────────────────────────────────────────────────────────────────

def run_token_keeper() -> dict:
    """
    Full token-keep cycle:
      1. Ping CLI → triggers OAuth token refresh if needed
      2. Read + encode the (refreshed) credentials file
      3. Push updated value to Railway Variables
      4. Return status dict

    Designed to run hourly. Never raises — all errors captured in return value.
    """
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ping_ok": False,
        "encode_ok": False,
        "railway_ok": False,
        "method": None,
        "message": "",
    }

    # Step 1 — ping CLI to trigger refresh
    ping_ok, ping_msg = _ping_cli_to_refresh()
    result["ping_ok"] = ping_ok
    if not ping_ok:
        result["message"] = f"CLI ping failed: {ping_msg}"
        _log(f"Token keeper: CLI ping failed — {ping_msg}")

        # NEW: If ping failed (token expired), attempt to restore from env var
        # before giving up. This breaks the stale-token cycle.
        _log("Token keeper: attempting credential restore from CLAUDE_SESSION_TOKEN env var…")
        try:
            from .pro_router import _try_restore_claude_auth
            if _try_restore_claude_auth():
                _log("Token keeper: credential restore SUCCESS — retrying ping…")
                ping_ok, ping_msg = _ping_cli_to_refresh()
                result["ping_ok"] = ping_ok
                if ping_ok:
                    _log("Token keeper: CLI ping OK after restore ✓")
                    time.sleep(2)
                else:
                    _log(f"Token keeper: CLI ping still failed after restore — {ping_msg}")
            else:
                _log("Token keeper: env var restore failed — trying full recovery chain…")
                try:
                    from .cli_auto_login import full_recovery_chain
                    if full_recovery_chain():
                        _log("Token keeper: full recovery chain SUCCESS ✓")
                        ping_ok = True
                        result["ping_ok"] = True
                        time.sleep(2)
                    else:
                        _log("Token keeper: full recovery chain FAILED — manual login required")
                except Exception as _re:
                    _log(f"Token keeper: recovery chain error — {_re}")
        except Exception as _e:
            _log(f"Token keeper: restore attempt error — {_e}")
    else:
        # Small delay to ensure file write completes after token refresh
        time.sleep(2)

    # Step 2 — read and encode credentials
    encoded, enc_msg = _read_and_encode_credentials()
    if not encoded:
        result["message"] = enc_msg
        _log(f"Token keeper: {enc_msg}")
        return result
    result["encode_ok"] = True

    # Step 2b — volume backup (primary persistence, no Railway API needed)
    # Writes raw credentials to /workspace/ which is mounted as a persistent volume.
    # entrypoint.sh reads this on boot before falling back to CLAUDE_SESSION_TOKEN env var.
    try:
        _vol = Path("/workspace/.claude_credentials_backup.json")
        _vol.write_bytes(_CREDS_FILE.read_bytes())
        _vol.chmod(0o600)
        _log("Token keeper: credentials backed up to volume (/workspace/.claude_credentials_backup.json) ✓")
        result["volume_ok"] = True
    except Exception as _ve:
        _log(f"Token keeper: volume backup failed — {_ve}")
        result["volume_ok"] = False

    # Step 3 — push to THIS service (super-agent) via API, CLI fallback
    ok, msg = _update_railway_variable("CLAUDE_SESSION_TOKEN", encoded)
    if ok:
        result["railway_ok"] = True
        result["method"]  = "api"
        result["message"] = msg
        _log("Token keeper: CLAUDE_SESSION_TOKEN refreshed for super-agent ✓")
    else:
        _log(f"Token keeper: Railway API failed ({msg}), trying CLI fallback...")
        ok2, msg2 = _update_via_cli("CLAUDE_SESSION_TOKEN", encoded)
        result["railway_ok"] = ok2
        result["method"]  = "cli" if ok2 else "none"
        result["message"] = msg2 if ok2 else f"API: {msg} | CLI: {msg2}"
        level = "refreshed and saved via CLI fallback" if ok2 else "FAILED to save to Railway"
        _log(f"Token keeper: CLAUDE_SESSION_TOKEN {level}. {result['message']}")
        if not ok2:
            try:
                from ..alerts.notifier import alert_token_refresh_failed
                alert_token_refresh_failed("Claude Pro", result["message"])
            except Exception:
                pass

    # Step 4 — also push to inspiring-cat (CLI worker) so its token never expires.
    # Set CLI_WORKER_SERVICE_ID in super-agent Railway Variables to inspiring-cat's
    # service ID (visible in Railway → inspiring-cat → Settings → Service ID).
    cli_worker_sid = os.environ.get("CLI_WORKER_SERVICE_ID", "")
    if cli_worker_sid and encoded:
        _log("Token keeper: pushing same token to inspiring-cat (CLI_WORKER_SERVICE_ID set)…")
        ok_cli, msg_cli = _update_railway_variable_for_service(
            "CLAUDE_SESSION_TOKEN", encoded, cli_worker_sid
        )
        if ok_cli:
            _log("Token keeper: inspiring-cat CLAUDE_SESSION_TOKEN refreshed ✓ — token will never expire on CLI worker.")
        else:
            _log(f"Token keeper: inspiring-cat token update FAILED — {msg_cli}. "
                 "Manually copy CLAUDE_SESSION_TOKEN from super-agent to inspiring-cat in Railway.")
    else:
        if not cli_worker_sid:
            _log("Token keeper: CLI_WORKER_SERVICE_ID not set — inspiring-cat token NOT refreshed. "
                 "Add CLI_WORKER_SERVICE_ID to super-agent Railway Variables to enable auto-refresh.")

    return result


def run_proactive_refresh() -> dict:
    """
    Proactive OAuth token renewal — runs every 12 hours even when CLI is healthy.

    Unlike run_token_keeper() which pings the CLI and relies on it to internally
    refresh the access token, this function:
      1. Attempts a direct OAuth refresh_token exchange (lightweight HTTP call)
      2. If that succeeds: persists updated credentials to volume + Railway
      3. If that fails (refresh_token dead): escalates to full_recovery_chain()

    Running proactively every 12 hours means the refresh token is exercised
    well before it could expire from inactivity (typical expiry: 30–90 days
    with zero activity), giving the self-healing system a clean recovery path.

    Never raises — all errors captured in return value.
    """
    result: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "direct_refresh_ok": False,
        "recovery_ok": False,
        "message": "",
    }

    _log("Proactive token refresh: starting scheduled 12-hour OAuth renewal...")

    try:
        from .cli_auto_login import _try_direct_refresh
        if _try_direct_refresh():
            _log("Proactive token refresh: direct OAuth refresh SUCCESS ✓ — credentials updated.")
            result["direct_refresh_ok"] = True
            result["message"] = "Direct OAuth refresh succeeded."
            return result
    except Exception as e:
        _log(f"Proactive token refresh: direct refresh error — {e}")

    # Direct refresh failed — credentials may be stale or refresh token expired.
    # Escalate to full recovery chain before things get worse.
    _log("Proactive token refresh: direct refresh failed — escalating to full recovery chain...")
    try:
        from .cli_auto_login import full_recovery_chain
        if full_recovery_chain():
            _log("Proactive token refresh: full recovery chain SUCCESS ✓")
            result["recovery_ok"] = True
            result["message"] = "Full recovery chain succeeded after direct refresh failed."
        else:
            _log("Proactive token refresh: full recovery chain FAILED — manual login may be required.")
            result["message"] = "Both direct refresh and full recovery chain failed."
            try:
                from .agent_status_tracker import mark_sick
                mark_sick("Claude CLI Pro")
            except Exception:
                pass
    except Exception as e:
        _log(f"Proactive token refresh: recovery chain error — {e}")
        result["message"] = f"Recovery chain exception: {e}"

    return result


def _log(msg: str) -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source="pro_token_keeper")
    except Exception:
        pass
