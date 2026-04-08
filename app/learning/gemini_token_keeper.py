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
    Call `gemini --version` to verify the CLI is alive and reachable.
    This also ensures the OAuth token is validated (Gemini CLI auto-refreshes
    access tokens from the stored refresh token on any invocation).
    """
    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True, text=True, timeout=_TIMEOUT,
            env={**os.environ, "HOME": "/root"},
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return True, output[:100]
        return False, f"Non-zero exit ({result.returncode}): {output[:200]}"
    except FileNotFoundError:
        return False, "gemini CLI not found — install: npm install -g @google/gemini-cli"
    except subprocess.TimeoutExpired:
        return False, "gemini --version timed out"
    except Exception as e:
        return False, f"CLI ping error: {e}"


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
        # Still attempt to save existing credentials even if ping failed
    else:
        time.sleep(2)  # allow file write to complete after any token refresh

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


def _log(msg: str) -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source="gemini_token_keeper")
    except Exception:
        pass
