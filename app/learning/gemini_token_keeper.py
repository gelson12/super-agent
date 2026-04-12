"""
Gemini Token Keeper — keeps Google Gemini CLI OAuth credentials alive indefinitely.

Mirrors pro_token_keeper.py exactly, for the Gemini CLI free-tier backup.

How it works:
  1. Runs daily at 04:00 UTC (1 hour after the Claude keeper at 03:00 UTC)
  2. Makes a lightweight `gemini --version` call to verify CLI is alive
  3. Re-encodes the credentials file as base64
  4. Pushes the updated value back to Railway Variables via the Railway API
     so the NEXT redeploy always gets fresh credentials

Self-perpetuating loop (same as Claude Pro):
  Container boot → decode GEMINI_SESSION_TOKEN → /root/.gemini/credentials.json
  Daily keeper   → verify + re-encode → Railway var updated
  Next boot      → decode fresh credentials → repeat forever

Setup (one-time, in VS Code terminal inside the running container):
  1. gemini auth login              ← follow Google OAuth flow in browser
  2. Find the credentials file:
       ls -la /root/.gemini/        ← look for credentials.json or oauth_creds.json
  3. Encode it:
       base64 -w0 /root/.gemini/credentials.json
  4. Copy the output → Railway Variables → GEMINI_SESSION_TOKEN
  5. Redeploy — the keeper takes over from here forever.

Requirements (same as Claude keeper):
  RAILWAY_TOKEN, RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID
"""
import base64
import json
import os
import subprocess
import time
from pathlib import Path

# Credentials directory — Gemini CLI stores OAuth credentials here
_GEMINI_DIR = Path("/root/.gemini")
# The keeper tries these filenames in order (covers different CLI versions)
_CREDS_CANDIDATES = [
    _GEMINI_DIR / "credentials.json",
    _GEMINI_DIR / "oauth_creds.json",
    _GEMINI_DIR / "auth.json",
]
_TIMEOUT = 30
_RAILWAY_VAR_NAME = "GEMINI_SESSION_TOKEN"


def _find_creds_file() -> Path | None:
    """Return the first existing credentials file candidate, or None."""
    for p in _CREDS_CANDIDATES:
        if p.exists():
            return p
    # Last resort: any .json file in the gemini dir
    if _GEMINI_DIR.is_dir():
        jsons = list(_GEMINI_DIR.glob("*.json"))
        if jsons:
            return jsons[0]
    return None


# ── Railway API (shared with pro_token_keeper) ────────────────────────────────

def _update_railway_variable(name: str, value: str) -> tuple[bool, str]:
    """Upsert a Railway service variable via the GraphQL API."""
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


# ── Gemini CLI ping ────────────────────────────────────────────────────────────

def _ping_gemini_cli() -> tuple[bool, str]:
    """
    Verify Gemini CLI is alive AND auth is valid by making a real API call.

    `gemini --version` only checks the binary, not auth. We need to force
    an actual API call so the OAuth library refreshes the access token
    from the stored refresh_token. This keeps the session alive indefinitely.
    """
    # Step 1: check binary exists
    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True, text=True, timeout=_TIMEOUT,
            env={**os.environ, "HOME": "/root"},
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            return False, f"Non-zero exit ({result.returncode}): {output[:200]}"
    except FileNotFoundError:
        return False, "gemini CLI not found — install: npm install -g @google/gemini-cli"
    except subprocess.TimeoutExpired:
        return False, "gemini --version timed out"
    except Exception as e:
        return False, f"CLI ping error: {e}"

    # Step 2: force a real API call to trigger OAuth token refresh
    try:
        result = subprocess.run(
            ["gemini", "--prompt", "Reply with only the word: OK"],
            capture_output=True, text=True, timeout=60,
            cwd="/workspace",
            env={**os.environ, "HOME": "/root"},
        )
        output = (result.stdout + result.stderr).strip()
        _auth_errors = (
            "please set an auth method",
            "failed to authenticate",
            "authentication error",
            "gemini_api_key",
        )
        if any(e in output.lower() for e in _auth_errors):
            return False, f"Auth failed: {output[:200]}"
        if result.returncode == 0 and output:
            return True, f"Auth OK — response: {output[:50]}"
        return False, f"Unexpected: rc={result.returncode} output={output[:200]}"
    except subprocess.TimeoutExpired:
        return False, "gemini prompt timed out (60s)"
    except Exception as e:
        return False, f"CLI auth check error: {e}"


def _read_and_encode_credentials() -> tuple[str | None, str]:
    """Read the Gemini credentials file and return base64-encoded value."""
    try:
        creds_file = _find_creds_file()
        if not creds_file:
            return None, (
                f"No credentials file found in {_GEMINI_DIR}. "
                "Run 'gemini auth login' in VS Code terminal to authenticate."
            )
        raw = creds_file.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return encoded, f"OK ({creds_file.name})"
    except Exception as e:
        return None, f"Failed to read credentials: {e}"


# ── Main job ───────────────────────────────────────────────────────────────────

def run_token_keeper() -> dict:
    """
    Full Gemini token-keep cycle (mirrors pro_token_keeper.run_token_keeper):
      1. Ping CLI → verifies OAuth is live, triggers silent token refresh
      2. Read + encode the credentials file
      3. Push updated value to Railway Variables (API → CLI fallback)
      4. Return status dict

    Designed to run daily at 04:00 UTC. Never raises.
    """
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ping_ok": False,
        "encode_ok": False,
        "railway_ok": False,
        "method": None,
        "message": "",
    }

    # Step 1 — ping CLI
    ping_ok, ping_msg = _ping_gemini_cli()
    result["ping_ok"] = ping_ok
    if not ping_ok:
        result["message"] = f"Gemini CLI ping failed: {ping_msg}"
        _log(f"Gemini token keeper: CLI ping failed — {ping_msg}")

        # Recovery: restore from env var then retry
        _log("Gemini token keeper: attempting restore from GEMINI_SESSION_TOKEN env var…")
        if _try_restore_gemini_auth():
            _log("Gemini token keeper: restore succeeded — retrying ping…")
            ping_ok, ping_msg = _ping_gemini_cli()
            result["ping_ok"] = ping_ok
            if ping_ok:
                _log("Gemini token keeper: CLI ping OK after restore ✓")
                time.sleep(2)
            else:
                _log(f"Gemini token keeper: ping still failed after restore — {ping_msg}")
        else:
            _log("Gemini token keeper: restore failed (env var stale or missing)")
    else:
        time.sleep(2)  # allow file write to complete after any token refresh
        # Proactive: also do a direct OAuth refresh to keep refresh_token alive
        # Google refresh tokens can expire if unused for 6 months (or sooner
        # if the project is in "testing" mode — 7 days). Regular refreshes
        # prevent expiry.
        try:
            _try_direct_gemini_refresh()
        except Exception:
            pass  # non-fatal — CLI call already refreshed the token

    # Step 2 — read and encode credentials
    encoded, enc_msg = _read_and_encode_credentials()
    if not encoded:
        result["message"] = enc_msg
        _log(f"Gemini token keeper: {enc_msg}")
        return result
    result["encode_ok"] = True

    # Step 3 — push to Railway (API first, CLI fallback)
    ok, msg = _update_railway_variable(_RAILWAY_VAR_NAME, encoded)
    if ok:
        result["railway_ok"] = True
        result["method"]  = "api"
        result["message"] = msg
        _log(
            f"Gemini token keeper: {_RAILWAY_VAR_NAME} refreshed and saved to Railway via API. "
            f"Gemini free-tier backup active for next redeploy."
        )
    else:
        _log(f"Gemini token keeper: Railway API failed ({msg}), trying CLI fallback...")
        ok2, msg2 = _update_via_cli(_RAILWAY_VAR_NAME, encoded)
        result["railway_ok"] = ok2
        result["method"]  = "cli" if ok2 else "none"
        result["message"] = msg2 if ok2 else f"API: {msg} | CLI: {msg2}"
        level = "saved via CLI fallback" if ok2 else "FAILED to save to Railway"
        _log(f"Gemini token keeper: {_RAILWAY_VAR_NAME} {level}. {result['message']}")
        if not ok2:
            try:
                from ..alerts.notifier import alert_token_refresh_failed
                alert_token_refresh_failed("Gemini CLI", result["message"])
            except Exception:
                pass

    return result


def _try_restore_gemini_auth() -> bool:
    """
    Restore Gemini CLI credentials from GEMINI_SESSION_TOKEN env var.
    Returns True if restored and CLI is now responsive.
    """
    token = os.environ.get("GEMINI_SESSION_TOKEN", "")
    if not token:
        _log("Gemini restore: GEMINI_SESSION_TOKEN not set.")
        return False

    try:
        decoded = base64.b64decode(token + "==")
    except Exception as e:
        _log(f"Gemini restore: base64 decode failed — {e}")
        return False

    _GEMINI_DIR.mkdir(parents=True, exist_ok=True)
    for fpath in _CREDS_CANDIDATES:
        try:
            fpath.write_bytes(decoded)
            fpath.chmod(0o600)
        except Exception:
            pass

    # Verify
    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "HOME": "/root"},
        )
        if result.returncode == 0:
            _log("Gemini restore: credentials restored and CLI responding ✓")
            try:
                from .agent_status_tracker import mark_done
                mark_done("Gemini CLI")
            except Exception:
                pass
            return True
    except Exception:
        pass

    _log("Gemini restore: credentials written but CLI still not responding.")
    try:
        from .agent_status_tracker import mark_sick
        mark_sick("Gemini CLI")
    except Exception:
        pass
    return False


def _try_direct_gemini_refresh() -> bool:
    """
    Attempt to refresh the Google OAuth token directly using the refresh_token
    from the credentials file. Google's OAuth2 endpoint is standard and well-documented.
    """
    try:
        creds_file = _find_creds_file()
        if not creds_file:
            return False

        creds = json.loads(creds_file.read_text())

        # Google credentials format: {"client_id", "client_secret", "refresh_token", ...}
        refresh_token = creds.get("refresh_token")
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")

        if not all([refresh_token, client_id, client_secret]):
            _log("Gemini direct refresh: missing refresh_token/client_id/client_secret in credentials.")
            return False

        _log("Gemini direct refresh: attempting Google OAuth token refresh…")

        data = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode()

        import urllib.request
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            new_access = result.get("access_token")
            new_refresh = result.get("refresh_token", refresh_token)

            if new_access:
                _log("Gemini direct refresh: got new access_token ✓")
                creds["access_token"] = new_access
                if new_refresh != refresh_token:
                    creds["refresh_token"] = new_refresh
                    _log("Gemini direct refresh: refresh_token also rotated.")

                creds_file.write_text(json.dumps(creds, indent=2))
                creds_file.chmod(0o600)

                # Push updated token to Railway
                encoded = base64.b64encode(creds_file.read_bytes()).decode("ascii")
                _update_railway_variable(_RAILWAY_VAR_NAME, encoded)
                _log("Gemini direct refresh: credentials refreshed and saved to Railway ✓")

                try:
                    from .agent_status_tracker import mark_done
                    mark_done("Gemini CLI")
                except Exception:
                    pass
                return True

        _log("Gemini direct refresh: no access_token in response.")
        return False

    except Exception as e:
        _log(f"Gemini direct refresh error: {e}")
        return False


def gemini_full_recovery() -> bool:
    """
    Full Gemini CLI recovery chain:
      1. Direct Google OAuth refresh (lightweight)
      2. Restore from env var
    Returns True if any method succeeded.
    """
    _log("=== Gemini recovery chain starting ===")

    # Attempt 1: Direct OAuth refresh
    if _try_direct_gemini_refresh():
        _log("=== Gemini recovery SUCCESS via direct refresh ===")
        return True

    # Attempt 2: Restore from env var
    if _try_restore_gemini_auth():
        _log("=== Gemini recovery SUCCESS via env var restore ===")
        return True

    _log("=== Gemini recovery FAILED — manual 'gemini auth login' required ===")
    return False


def _log(msg: str) -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source="gemini_token_keeper")
    except Exception:
        pass
