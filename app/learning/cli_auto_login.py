"""
Automated Claude CLI login using Playwright headless browser.

When the Claude CLI session token expires and the refresh token is no longer
valid, this module performs a full OAuth re-authentication automatically:

  1. Runs `claude login` to get the OAuth URL
  2. Opens headless Chromium via Playwright
  3. Navigates to the OAuth URL
  4. Logs into claude.ai with stored credentials (ANTHROPIC_EMAIL / ANTHROPIC_PASSWORD)
  5. Clicks "Approve" on the consent screen
  6. CLI detects the localhost callback and saves the new token
  7. Encodes and pushes the fresh token to Railway Variables

Claude.ai uses PASSWORDLESS auth (email + verification code sent to inbox).
The flow is:
  1. Navigate to OAuth URL
  2. Enter email → click "Continue with email"
  3. Wait for verification code email to arrive
  4. Read code via n8n webhook (monitors Hotmail inbox)
  5. Type code → click "Verify Email Address"
  6. Click "Approve" on consent screen

Requirements (Railway env vars):
  ANTHROPIC_EMAIL      — claude.ai account email (e.g. gelson_m@hotmail.com)
  N8N_BASE_URL         — n8n instance URL (for triggering email monitor workflow)

The n8n workflow monitors the Hotmail inbox for Anthropic verification emails,
extracts the 6-digit code, and POSTs it to the CLI worker's
/webhook/verification-code endpoint.

This is the nuclear recovery option — called only when:
  - The refresh token itself is dead (can't be auto-refreshed)
  - _try_restore_claude_auth() failed (env var has stale token too)
  - Token keeper and watchdog both exhausted their recovery paths

Public API:
    auto_login_claude()  → bool  (True if login succeeded)
"""
import os
import re
import subprocess
import time
import threading
import base64
from pathlib import Path


_CREDS_FILE = Path("/root/.claude/.credentials.json")
_TIMEOUT_LOGIN = 120  # max seconds for the entire login flow
_log_lock = threading.Lock()


def _log(msg: str) -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source="cli_auto_login")
    except Exception:
        pass


# ── Verification code exchange ────────────────────────────────────────────────
# The Playwright script waits for an n8n webhook to POST the verification code.
# Thread-safe queue used to pass the code from the webhook handler to the
# waiting browser automation thread.
import queue
_verification_code_queue: queue.Queue = queue.Queue()
_VERIFICATION_CODE_TIMEOUT = 120  # max seconds to wait for code from n8n


def receive_verification_code(code: str) -> None:
    """Called by the webhook endpoint when n8n sends the verification code."""
    _log(f"Received verification code from n8n webhook: {code[:2]}****")
    _verification_code_queue.put(code)


def _wait_for_verification_code() -> str | None:
    """Block until verification code arrives from n8n, or timeout."""
    _log(f"Waiting for verification code from n8n (timeout: {_VERIFICATION_CODE_TIMEOUT}s)...")
    try:
        code = _verification_code_queue.get(timeout=_VERIFICATION_CODE_TIMEOUT)
        return code.strip()
    except queue.Empty:
        _log("Verification code TIMEOUT — n8n did not send the code in time.")
        return None


def _trigger_n8n_email_monitor() -> bool:
    """Trigger the n8n workflow that monitors Hotmail for Anthropic verification emails."""
    try:
        n8n_base = os.environ.get("N8N_BASE_URL", "")
        if not n8n_base:
            _log("Cannot trigger n8n email monitor — N8N_BASE_URL not set.")
            return False

        # Trigger the webhook on the n8n workflow
        import urllib.request
        import json
        webhook_url = f"{n8n_base}/webhook/claude-verification-monitor"
        data = json.dumps({"action": "start_monitoring", "email": os.environ.get("ANTHROPIC_EMAIL", "")}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            _log(f"n8n email monitor triggered: {resp.status}")
            return resp.status == 200
    except Exception as e:
        _log(f"Failed to trigger n8n email monitor: {e}")
        return False


def auto_login_claude() -> bool:
    """
    Full automated OAuth login flow using headless browser + n8n email monitor.
    Returns True if login succeeded and credentials file was updated.
    """
    email = os.environ.get("ANTHROPIC_EMAIL", "")
    if not email:
        _log("Auto-login skipped — ANTHROPIC_EMAIL not set in Railway Variables.")
        return False

    _log(f"Starting automated Claude CLI login for {email}...")

    # Track dashboard status
    try:
        from .agent_status_tracker import mark_working
        mark_working("Claude CLI Pro", "Auto-login in progress...")
    except Exception:
        pass

    try:
        return _do_auto_login(email)
    except Exception as e:
        _log(f"Auto-login failed with error: {e}")
        try:
            from .agent_status_tracker import mark_sick
            mark_sick("Claude CLI Pro")
        except Exception:
            pass
        return False


def _do_auto_login(email: str) -> bool:
    """Core login flow — start CLI, capture URL, automate browser with n8n verification."""

    # Step 1: Start `claude login` subprocess
    _log("Step 1: Starting `claude login` subprocess...")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HOME"] = "/root"

    proc = subprocess.Popen(
        ["claude", "login"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        env=env,
        cwd="/workspace",
    )

    # Step 2: Capture the OAuth URL from stdout
    _log("Step 2: Waiting for OAuth URL from CLI...")
    oauth_url = None
    deadline = time.time() + 30  # 30s to get the URL

    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue

        _log(f"CLI output: {line.strip()[:200]}")

        # Look for the OAuth URL
        url_match = re.search(r'(https://claude\.ai/oauth[^\s"\']+)', line)
        if not url_match:
            url_match = re.search(r'(https://[^\s"\']*authorize[^\s"\']+)', line)
        if url_match:
            oauth_url = url_match.group(1)
            _log(f"Step 2: Got OAuth URL: {oauth_url[:100]}...")
            break

        # Sometimes CLI says "Open this URL" with the URL on the next line
        if "open" in line.lower() and "url" in line.lower():
            next_line = proc.stdout.readline()
            if next_line:
                url_match = re.search(r'(https://[^\s"\']+)', next_line)
                if url_match:
                    oauth_url = url_match.group(1)
                    _log(f"Step 2: Got OAuth URL from next line: {oauth_url[:100]}...")
                    break

    if not oauth_url:
        _log("Step 2 FAILED: Could not extract OAuth URL from CLI output. Killing process.")
        proc.kill()
        return False

    # Step 3: Trigger n8n email monitor BEFORE opening browser
    # n8n will start watching for the Anthropic verification email
    _log("Step 3a: Triggering n8n email monitor...")
    _trigger_n8n_email_monitor()

    # Step 3b: Automate the browser flow
    _log("Step 3b: Opening headless browser...")
    browser_ok = _automate_browser(oauth_url, email)

    if not browser_ok:
        _log("Step 3 FAILED: Browser automation failed.")
        proc.kill()
        return False

    # Step 4: Wait for CLI to finish (it should detect the callback)
    _log("Step 4: Waiting for CLI to complete login...")
    try:
        # Send Enter key in case CLI is waiting for it
        proc.stdin.write("\n")
        proc.stdin.flush()
    except Exception:
        pass

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _log("Step 4: CLI didn't exit in 30s — killing (token may still be saved).")
        proc.kill()

    # Step 5: Verify the token was saved
    _log("Step 5: Verifying credentials...")
    time.sleep(2)  # Give CLI time to write the file

    if _CREDS_FILE.exists():
        try:
            r = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True, text=True, timeout=15,
                env=env,
            )
            if '"authMethod": "claude.ai"' in r.stdout or '"authMethod":"claude.ai"' in r.stdout.replace(": ", ":"):
                _log("Step 5: LOGIN SUCCESS — Claude CLI Pro authenticated ✓")

                # Step 6: Push updated token to Railway
                _push_token_to_railway()

                try:
                    from .agent_status_tracker import mark_done
                    mark_done("Claude CLI Pro")
                except Exception:
                    pass
                return True
        except Exception as e:
            _log(f"Step 5: Auth verify error — {e}")

    _log("Step 5 FAILED: Credentials file not found or auth invalid after login.")
    try:
        from .agent_status_tracker import mark_sick
        mark_sick("Claude CLI Pro")
    except Exception:
        pass
    return False


def _automate_browser(oauth_url: str, email: str) -> bool:
    """
    Automate the Claude.ai passwordless OAuth login flow:
      1. Navigate to OAuth URL
      2. Enter email → click "Continue with email"
      3. Wait for verification code from n8n (via webhook → queue)
      4. Type code → click "Verify Email Address"
      5. Click "Approve" on consent screen
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log("Playwright not installed — cannot auto-login.")
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            context = browser.new_context()
            page = context.new_page()

            _log(f"Browser: navigating to OAuth URL...")
            page.goto(oauth_url, timeout=30000)
            time.sleep(3)
            _log(f"Browser: on page = {page.url[:100]}")

            # ── Step 1: Enter email ──────────────────────────────────────
            email_field = (
                page.query_selector('input[type="email"]')
                or page.query_selector('input[placeholder*="email" i]')
                or page.query_selector('input[name="email"]')
            )
            if email_field:
                email_field.fill(email)
                _log(f"Browser: email entered ({email})")
                time.sleep(0.5)

                # Click "Continue with email"
                continue_btn = (
                    page.query_selector('button:has-text("Continue with email")')
                    or page.query_selector('button:has-text("Continue")')
                    or page.query_selector('button[type="submit"]')
                )
                if continue_btn:
                    continue_btn.click()
                    _log("Browser: clicked 'Continue with email'")
                else:
                    page.keyboard.press("Enter")
                    _log("Browser: pressed Enter to submit email")

                time.sleep(3)
                _log(f"Browser: after email submit, URL = {page.url[:100]}")
            else:
                _log("Browser: no email field found on page.")
                _log(f"Browser: page content sample: {page.content()[:500]}")
                browser.close()
                return False

            # ── Step 2: Wait for verification code from n8n ──────────────
            # The n8n workflow was triggered before the browser opened.
            # It monitors the Hotmail inbox and will POST the code to our webhook.
            verification_field = (
                page.query_selector('input[placeholder*="verification" i]')
                or page.query_selector('input[placeholder*="code" i]')
                or page.query_selector('input[type="text"]')
                or page.query_selector('input[type="number"]')
            )
            if not verification_field:
                _log("Browser: no verification code field found — page may have changed.")
                _log(f"Browser: page content: {page.content()[:500]}")
                # Maybe already approved (Google SSO or remembered session)
                if _click_approve(page):
                    browser.close()
                    return True
                browser.close()
                return False

            _log("Browser: verification code field found — waiting for code from n8n...")
            code = _wait_for_verification_code()
            if not code:
                _log("Browser: no verification code received — aborting.")
                browser.close()
                return False

            # ── Step 3: Enter code and verify ────────────────────────────
            verification_field.fill(code)
            _log(f"Browser: entered verification code ({code[:2]}****)")
            time.sleep(0.5)

            verify_btn = (
                page.query_selector('button:has-text("Verify")')
                or page.query_selector('button:has-text("Submit")')
                or page.query_selector('button:has-text("Continue")')
                or page.query_selector('button[type="submit"]')
            )
            if verify_btn:
                verify_btn.click()
                _log("Browser: clicked Verify button")
            else:
                page.keyboard.press("Enter")
                _log("Browser: pressed Enter to verify")

            time.sleep(5)
            _log(f"Browser: after verification, URL = {page.url[:100]}")

            # ── Step 4: Click Approve on consent screen ──────────────────
            _log("Browser: looking for Approve button...")
            _click_approve(page)
            time.sleep(3)
            _log(f"Browser: final URL = {page.url[:100]}")

            browser.close()
            return True

    except Exception as e:
        _log(f"Browser automation error: {e}")
        return False


def _click_approve(page) -> bool:
    """Find and click the OAuth Approve/Allow button."""
    try:
        selectors = [
            'button:has-text("Approve")',
            'button:has-text("Allow")',
            'button:has-text("Authorize")',
            'button:has-text("Accept")',
            'button:has-text("Grant")',
            'button:has-text("Yes")',
            'input[type="submit"][value*="Approve" i]',
            'input[type="submit"][value*="Allow" i]',
        ]
        for sel in selectors:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                _log(f"Browser: clicked '{sel}'")
                return True

        # If no button found, the page might have auto-approved
        # Check if we're already on a success/callback page
        if "localhost" in page.url or "callback" in page.url:
            _log("Browser: already redirected to callback — auto-approved.")
            return True

        _log("Browser: no approve button found. Page content sample: " + page.content()[:300])
        return False

    except Exception as e:
        _log(f"Browser: approve click error — {e}")
        return False


def _push_token_to_railway() -> None:
    """Encode credentials and push to Railway Variables."""
    try:
        if not _CREDS_FILE.exists():
            return
        raw = _CREDS_FILE.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")

        from .pro_token_keeper import _update_railway_variable, _update_via_cli
        ok, msg = _update_railway_variable("CLAUDE_SESSION_TOKEN", encoded)
        if ok:
            _log("Token pushed to Railway Variables via API ✓")
        else:
            ok2, msg2 = _update_via_cli("CLAUDE_SESSION_TOKEN", encoded)
            if ok2:
                _log("Token pushed to Railway Variables via CLI ✓")
            else:
                _log(f"FAILED to push token to Railway: API={msg}, CLI={msg2}")

        # Also push to inspiring-cat if configured
        cli_worker_sid = os.environ.get("CLI_WORKER_SERVICE_ID", "")
        if cli_worker_sid:
            from .pro_token_keeper import _update_railway_variable_for_service
            ok3, msg3 = _update_railway_variable_for_service(
                "CLAUDE_SESSION_TOKEN", encoded, cli_worker_sid
            )
            if ok3:
                _log("Token also pushed to inspiring-cat ✓")

    except Exception as e:
        _log(f"Token push error: {e}")


def _try_direct_refresh() -> bool:
    """
    Attempt to refresh the token using the OAuth refresh_token directly,
    bypassing the CLI entirely. This works when the refresh_token is still
    valid but the access_token has expired.

    The Claude CLI credentials file contains an OAuth session with a
    refresh_token. We can use it to get a new access_token via the
    standard OAuth token endpoint.
    """
    try:
        import json
        import urllib.request

        if not _CREDS_FILE.exists():
            _log("Direct refresh: no credentials file found.")
            return False

        creds = json.loads(_CREDS_FILE.read_text())

        # Extract refresh token — the structure varies by CLI version
        refresh_token = (
            creds.get("refreshToken")
            or creds.get("refresh_token")
            or creds.get("oauthRefreshToken")
        )
        if not refresh_token:
            # Try nested structures
            oauth = creds.get("oauth", {})
            refresh_token = oauth.get("refreshToken") or oauth.get("refresh_token")

        if not refresh_token:
            _log("Direct refresh: no refresh_token found in credentials file.")
            return False

        _log("Direct refresh: found refresh_token — attempting OAuth refresh...")

        # Claude.ai uses standard OAuth2 refresh flow
        # The token endpoint and client_id can be extracted from the credentials
        client_id = creds.get("clientId") or creds.get("client_id") or "claude-cli"

        # Try the Anthropic OAuth token endpoint
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }).encode()

        # Try known Anthropic OAuth endpoints
        endpoints = [
            "https://claude.ai/api/oauth/token",
            "https://api.anthropic.com/oauth/token",
            "https://auth.anthropic.com/oauth/token",
        ]

        for endpoint in endpoints:
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode())
                    new_access = result.get("access_token")
                    new_refresh = result.get("refresh_token", refresh_token)

                    if new_access:
                        _log(f"Direct refresh SUCCESS via {endpoint}")
                        # Update credentials file with new tokens
                        if "accessToken" in creds:
                            creds["accessToken"] = new_access
                        elif "access_token" in creds:
                            creds["access_token"] = new_access
                        if "refreshToken" in creds:
                            creds["refreshToken"] = new_refresh
                        elif "refresh_token" in creds:
                            creds["refresh_token"] = new_refresh
                        if "oauth" in creds:
                            creds["oauth"]["accessToken"] = new_access
                            creds["oauth"]["refreshToken"] = new_refresh

                        _CREDS_FILE.write_text(json.dumps(creds, indent=2))
                        _CREDS_FILE.chmod(0o600)
                        _push_token_to_railway()
                        return True

            except urllib.error.HTTPError as e:
                _log(f"Direct refresh: {endpoint} returned HTTP {e.code}")
                continue
            except Exception as e:
                _log(f"Direct refresh: {endpoint} error — {e}")
                continue

        _log("Direct refresh: all OAuth endpoints failed.")
        return False

    except Exception as e:
        _log(f"Direct refresh error: {e}")
        return False


def full_recovery_chain() -> bool:
    """
    Complete recovery chain — try everything in order:
      1. Direct OAuth refresh (lightweight, no browser)
      2. Restore from env var (in case it was updated externally)
      3. Full browser auto-login (nuclear option)

    Returns True if ANY method succeeded.
    """
    _log("=== Starting full CLI recovery chain ===")

    # Attempt 1: Direct OAuth refresh
    _log("Recovery attempt 1/3: Direct OAuth refresh...")
    if _try_direct_refresh():
        _log("=== Recovery SUCCESS via direct refresh ===")
        return True

    # Attempt 2: Restore from env var
    _log("Recovery attempt 2/3: Restore from env var...")
    try:
        from .pro_router import _try_restore_claude_auth
        if _try_restore_claude_auth():
            _log("=== Recovery SUCCESS via env var restore ===")
            return True
    except Exception as e:
        _log(f"Env var restore failed: {e}")

    # Attempt 3: Full browser auto-login
    _log("Recovery attempt 3/3: Full browser auto-login...")
    if auto_login_claude():
        _log("=== Recovery SUCCESS via browser auto-login ===")
        return True

    _log("=== ALL recovery methods FAILED — manual login required ===")
    return False
