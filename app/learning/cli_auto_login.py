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

# Prevent concurrent recovery chains. When the token expires, the watchdog,
# router, AND token_keeper all fire simultaneously — without this lock they
# all enter the browser and submit the email at the same time, which triggers
# Anthropic's "error sending login link" rate-limit error.
_recovery_lock = threading.Lock()
_recovery_running = threading.Event()  # set while a recovery is in progress
_recovery_started_at: float = 0.0     # mtime threshold: creds written after this = fresh

# Active PTY master fd — set while claude login is running so the manual-auth-code
# webhook can write the code directly to the PTY stdin without needing terminal paste.
_active_pty_master_fd: int | None = None
_active_oauth_url: str = ""  # the OAuth URL currently waiting for auth

# Volume paths that survive container restarts (Railway volume mount at /workspace)
_COOKIES_FILE = Path("/workspace/.claude_browser_cookies.json")
_RATELIMIT_FILE = Path("/workspace/.claude_login_ratelimit")
# Escalating backoff: hit_count → cooldown seconds
# 1st hit = 1h, 2nd hit = 4h, 3rd+ hit = 24h
# Prevents accumulating rate-limit damage during sustained failure periods.
_RATELIMIT_BACKOFF = {1: 3600, 2: 14400, 3: 86400}
_RATELIMIT_DEFAULT_COOLDOWN = 86400  # 24h for any hit beyond index 3


def _record_ratelimit_hit() -> int:
    """
    Record a rate-limit hit and return the cooldown seconds chosen.
    File format: "<expiry_timestamp> <hit_count>"
    Escalating backoff: hit 1→1h, hit 2→4h, hit 3+→24h.
    Also pushes the expiry to a Railway env var as a cross-restart backup.
    """
    hit_count = 1
    try:
        if _RATELIMIT_FILE.exists():
            parts = _RATELIMIT_FILE.read_text().strip().split()
            hit_count = int(parts[1]) + 1 if len(parts) >= 2 else 2
    except Exception:
        pass

    cooldown = _RATELIMIT_BACKOFF.get(hit_count, _RATELIMIT_DEFAULT_COOLDOWN)
    expiry = time.time() + cooldown
    try:
        _RATELIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RATELIMIT_FILE.write_text(f"{expiry} {hit_count}")
    except Exception as _fe:
        _log(f"Rate-limit file write failed: {_fe}")

    _log(f"Rate-limit hit #{hit_count} recorded — cooldown {cooldown // 3600:.0f}h "
         f"(next attempt after {time.strftime('%H:%M UTC', time.gmtime(expiry))})")

    # Best-effort: push expiry to Railway env var so it survives if /workspace is not mounted
    try:
        from .pro_token_keeper import _update_railway_variable
        _update_railway_variable("CLAUDE_LOGIN_RATELIMIT_UNTIL", f"{expiry} {hit_count}")
    except Exception:
        pass

    return cooldown


def _check_ratelimit() -> float:
    """
    Return seconds remaining on cooldown (>0 = blocked), or 0 if clear.
    Checks both the file and the Railway env var backup.
    """
    def _parse(text: str) -> tuple[float, int]:
        parts = text.strip().split()
        exp = float(parts[0]) if parts else 0.0
        cnt = int(parts[1]) if len(parts) >= 2 else 1
        return exp, cnt

    expiry = 0.0
    # Primary: file
    try:
        if _RATELIMIT_FILE.exists():
            expiry, _ = _parse(_RATELIMIT_FILE.read_text())
    except Exception:
        pass

    # Backup: Railway env var (in case file was lost after container restart)
    if expiry < time.time():
        try:
            env_val = os.environ.get("CLAUDE_LOGIN_RATELIMIT_UNTIL", "")
            if env_val:
                env_exp, env_cnt = _parse(env_val)
                if env_exp > expiry:
                    expiry = env_exp
                    # Restore the file so subsequent checks don't need the env var
                    try:
                        _RATELIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
                        _RATELIMIT_FILE.write_text(f"{env_exp} {env_cnt}")
                    except Exception:
                        pass
        except Exception:
            pass

    remaining = expiry - time.time()
    return remaining if remaining > 0 else 0.0


def _log(msg: str) -> None:
    # Always print so Railway container logs capture it (bg_log only writes to local file)
    print(f"[cli_auto_login] {msg}", flush=True)
    try:
        from ..activity_log import bg_log
        bg_log(msg, source="cli_auto_login")
    except Exception:
        pass


# ── Magic link exchange ───────────────────────────────────────────────────────
# Claude.ai passwordless auth sends a MAGIC LINK email, NOT a 6-digit code.
# The Playwright script waits for n8n to POST the magic link URL.
# Thread-safe queue used to pass the URL from the webhook handler to the
# waiting browser automation thread.
import queue
_verification_code_queue: queue.Queue = queue.Queue()  # carries the magic link URL
_VERIFICATION_CODE_TIMEOUT = 180  # max seconds to wait for magic URL from n8n
# 180s = 3 minutes: email delivery (~15s) + n8n poll interval (up to 60s) + POST + buffer

_manual_auth_code_queue: queue.Queue = queue.Queue()  # carries manual auth code from user
_MANUAL_AUTH_CODE_TIMEOUT = 1800  # 30 minutes for user to complete browser login manually


def receive_verification_code(code: str) -> None:
    """
    Called by the webhook endpoint when n8n sends the magic link URL
    (or a 6-digit code if Claude ever reverts to code-based auth).
    The name is kept for backward-compatibility with all webhook endpoints.
    """
    _log(f"Received auth payload from n8n webhook: {code[:40]}...")
    _verification_code_queue.put(code)


def send_manual_auth_code(code: str) -> dict:
    """
    Deliver an auth code to the waiting auto_login_claude() call.

    Two delivery paths (both attempted):
    1. Queue  — auto_login_claude() is blocking on _manual_auth_code_queue after
                browser automation failed; the queue unblocks it and it writes the
                code to the PTY itself (process still alive).
    2. Direct — write to PTY fd immediately as a fallback if queue is not being waited on.
    """
    global _active_pty_master_fd, _active_oauth_url
    code = code.strip()

    # Path 1: put in queue so auto_login_claude() can handle it cleanly
    _manual_auth_code_queue.put(code)
    _log(f"Manual auth code queued (len={len(code)}): {code[:20]}...")
    _active_oauth_url = ""
    return {"ok": True, "message": "Auth code queued — auto_login_claude() will write it to PTY"}


def get_active_oauth_url() -> str:
    """Return the OAuth URL currently waiting for auth, or empty string."""
    return _active_oauth_url


def _wait_for_verification_code() -> str | None:
    """Block until magic link URL arrives from n8n, or timeout."""
    _log(f"Waiting for magic link URL from n8n (timeout: {_VERIFICATION_CODE_TIMEOUT}s)...")
    try:
        code = _verification_code_queue.get(timeout=_VERIFICATION_CODE_TIMEOUT)
        return code.strip()
    except queue.Empty:
        _log("Magic link TIMEOUT — n8n did not send the URL in time.")
        return None


def _trigger_n8n_email_monitor() -> bool:
    """Trigger the n8n workflow that monitors Hotmail for Anthropic verification emails."""
    try:
        n8n_base = os.environ.get("N8N_BASE_URL", "")
        if not n8n_base:
            _log("Cannot trigger n8n email monitor — N8N_BASE_URL not set.")
            return False

        # Poke the n8n workflow webhook so it starts a fresh poll cycle immediately.
        # Non-fatal: the Claude-Verification-Monitor workflow (jxnZZwTqJ7naPKc6) uses an
        # Outlook trigger and polls automatically — this just shortens the wait.
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


def _start_claude_login_pty(env: dict) -> tuple:
    """
    Launch `claude login` inside a pseudo-terminal so it thinks it has a real TTY.

    Without a PTY, the Claude CLI detects a non-interactive environment and refuses
    to start the OAuth URL flow ("OAuth authentication is currently not supported.").
    With a PTY it behaves exactly as if a human ran it in a terminal and prints the
    OAuth URL to stdout.

    Returns (oauth_url, proc, master_fd) on success, (None, None, None) on failure.
    The caller is responsible for closing master_fd when done.
    """
    try:
        import pty
        import select as _select
        import os as _os
    except ImportError as _ie:
        _log(f"PTY unavailable (not Linux?): {_ie} — cannot run claude login with TTY.")
        return None, None, None

    global _active_pty_master_fd
    _log("PTY: opening pseudo-terminal pair...")
    try:
        master_fd, slave_fd = pty.openpty()
    except Exception as _pe:
        _log(f"PTY: pty.openpty() failed: {_pe}")
        return None, None, None

    # Set terminal window to 250 columns so long OAuth URLs are never line-wrapped.
    # Without this the default 80-col width wraps the URL mid-string, causing the
    # regex to stop at the newline and capture only a truncated fragment.
    try:
        import fcntl, termios, struct
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 500, 0, 0))
        _log("PTY: window size set to 500 cols (prevents URL line-wrapping)")
    except Exception as _we:
        _log(f"PTY: could not set window size (non-fatal): {_we}")

    _active_pty_master_fd = master_fd
    _log(f"PTY: master_fd={master_fd} slave_fd={slave_fd}")

    try:
        import os as _os2
        proc = subprocess.Popen(
            ["claude", "login"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd="/workspace",
            close_fds=True,
        )
        # Close the slave end in the parent — only the child needs it
        _os2.close(slave_fd)
        slave_fd = -1
        _log(f"PTY: `claude login` launched (pid={proc.pid}) — reading output...")
    except Exception as _launch_e:
        _log(f"PTY: Popen failed: {_launch_e}")
        try: _os.close(master_fd)
        except Exception: pass
        try: _os.close(slave_fd)
        except Exception: pass
        return None, None, None

    # ── Read PTY output and look for OAuth URL ────────────────────────────────
    #
    # ANSI stripping: we must NOT blindly strip OSC sequences with
    #   r'\x1b\][^\x07]*\x07'
    # because the Claude CLI wraps the OAuth URL in an OSC 8 terminal hyperlink:
    #   ESC ] 8 ;; URL BEL  display_text  ESC ] 8 ;; BEL
    # That regex would eat the URL entirely.  Instead we use a two-step clean:
    #   1. Extract the URL from OSC 8 hyperlinks and keep it as plain text.
    #   2. Strip all other ANSI/VT control sequences.
    def _clean_pty(raw: str) -> str:
        # Step 1: OSC 8 hyperlinks — replace with just the URL
        s = re.sub(
            r'\x1b\]8;;([^\x07\x1b]*)\x07[^\x1b]*\x1b\]8;;\x07',
            r' \1 ',
            raw,
            flags=re.DOTALL,
        )
        # Step 2: remaining CSI sequences (colors, cursor moves, etc.)
        s = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', s)
        # Step 3: remaining OSC sequences (title sets, etc.)
        s = re.sub(r'\x1b\][^\x07]*\x07', '', s)
        # Step 4: carriage returns
        s = s.replace('\r', '')
        return s

    accumulated = ""      # ANSI-cleaned text (URLs extracted from OSC 8)
    accumulated_raw = ""  # raw bytes as string — fallback URL search target
    deadline = time.time() + 60  # max 60 s to get the URL
    oauth_url = None

    _URL_PATTERNS = [
        # claude.com (current CLI domain)
        re.compile(r'https://claude\.com/cai/oauth/authorize\?[^\s\r\n\x1b"\'<> ]+', re.I),
        re.compile(r'https://claude\.com/[^\s\r\n\x1b"\'<> ]*oauth[^\s\r\n\x1b"\'<> ]+', re.I),
        # claude.ai (legacy / fallback)
        re.compile(r'https://claude\.ai/oauth/authorize\?[^\s\r\n\x1b"\'<> ]+', re.I),
        re.compile(r'https://claude\.ai/[^\s\r\n\x1b"\'<> ]*oauth[^\s\r\n\x1b"\'<> ]+', re.I),
        re.compile(r'https://[^\s\r\n\x1b"\'<> ]{30,}', re.I),   # wide net fallback
    ]

    import os as _os3
    import select as _sel

    # Onboarding prompts the CLI shows on first run — we must advance past all of
    # them before it reaches the actual OAuth URL output.
    # Each entry: (unique_marker_in_output, bytes_to_send)
    # ORDER MATTERS: higher-priority / earlier prompts come first.
    # React Ink (used by claude CLI) runs in raw terminal mode.
    # In raw mode, Enter/Return sends \r (0x0D), NOT \n (0x0A).
    # Sending \n is like pressing Ctrl-J — the selector ignores it entirely.
    _ENTER = b"\r"

    _ONBOARDING_RESPONSES = [
        ("WelcometoClaude",         _ENTER),   # splash screen
        ("Choosethetextstyle",      _ENTER),   # text theme selector (❯1.Darkmode✔)
        ("Choosethesyntaxtheme",    _ENTER),   # syntax theme selector if separate
        ("Syntaxtheme:",            _ENTER),   # after syntax demo, press Enter
        # Login method selector — appears after theme setup:
        #   ❯1 Claude account with subscription · Pro, Max, Team, or Enterprise
        #    2 Anthropic Console account · API usage billing
        #    3 3rd-party platform ...
        # Option 1 is already highlighted (❯); pressing Enter confirms it.
        ("Selectloginmethod:",      _ENTER),
        ("❯1Claudeaccount",         _ENTER),   # same screen, alternative marker
        ("Pressanykeyto",           _ENTER),   # generic "press any key" prompts
        ("pressEnterto",            _ENTER),
        ("Tologincontinue",         _ENTER),
        ("Continuewithoutsigning",  _ENTER),
    ]

    # Cooldown: at most one Enter per 2 seconds.
    # This prevents two markers appearing in the same large chunk from both
    # firing immediately — e.g. 'Choosethetextstyle' and 'Syntaxtheme:' can
    # both appear in one output chunk; we handle the first, then the second
    # fires on the next idle cycle after the CLI redraws.
    _last_response_time = 0.0
    _RESPONSE_COOLDOWN = 2.0  # seconds

    def _maybe_respond(text_no_spaces: str) -> bool:
        """Send Enter for the first matching prompt. Returns True if sent."""
        nonlocal _last_response_time
        now = time.time()
        if now - _last_response_time < _RESPONSE_COOLDOWN:
            return False  # still in cooldown — try again next cycle
        for marker, response in _ONBOARDING_RESPONSES:
            if marker in text_no_spaces:
                _log(f"PTY: onboarding prompt detected ({marker!r}) — sending Enter...")
                try:
                    _os3.write(master_fd, response)
                    _last_response_time = now
                except Exception as _we:
                    _log(f"PTY: write error during onboarding: {_we}")
                return True
        return False

    while time.time() < deadline:
        # Check if the process has already exited (error path)
        if proc.poll() is not None:
            # Drain any remaining output
            try:
                while True:
                    r, _, _ = _sel.select([master_fd], [], [], 0.1)
                    if not r:
                        break
                    _dc = _os3.read(master_fd, 4096).decode("utf-8", errors="replace")
                    accumulated += _clean_pty(_dc)
                    accumulated_raw += _dc
            except Exception:
                pass
            rc_now = proc.poll()
            _log(f"PTY: process exited (rc={rc_now}) before URL found.")
            _log(f"PTY: Accumulated (cleaned, last 800): {accumulated[-800:]!r}")
            _log(f"PTY: Accumulated (raw, last 400): {accumulated_raw[-400:]!r}")
            break

        try:
            r, _, _ = _sel.select([master_fd], [], [], 1.0)
        except Exception as _se:
            _log(f"PTY: select error: {_se}")
            break

        if not r:
            # No new data for 1 s — check the tail of accumulated output for
            # a stuck prompt and nudge with Enter (respects cooldown).
            #
            # CRITICAL: strip whitespace BEFORE slicing.
            # The terminal TUI pads every line to full width with spaces.
            # accumulated[-400:] is mostly trailing spaces; after .replace(" ","")
            # only ~50 real chars remain and the prompt marker is out of reach.
            # Stripping first gives us 400 chars of *actual content*.
            _stripped_acc = accumulated.replace(" ", "").replace("\n", "")
            _tail = _stripped_acc[-400:]
            _maybe_respond(_tail)
            continue

        try:
            raw = _os3.read(master_fd, 4096)
        except OSError as _ose:
            # EIO = slave side closed (process exited)
            _log(f"PTY: read OSError (process likely exited): {_ose}")
            break

        chunk = raw.decode("utf-8", errors="replace")
        clean = _clean_pty(chunk)
        accumulated += clean
        accumulated_raw += chunk

        # Log intermediate output for diagnosis
        if clean.strip():
            _log(f"PTY output: {clean.strip()[:300]!r}")

        # Respond to prompts in the fresh chunk (one Enter per cooldown window).
        _chunk_nows = clean.replace(" ", "").replace("\n", "")
        _maybe_respond(_chunk_nows)

        # Search for OAuth URL in BOTH cleaned and raw accumulated text.
        # Cleaned: handles plain-text URLs with ANSI color codes stripped.
        # Raw: catches URLs inside OSC 8 hyperlinks that _clean_pty may have
        #      partially handled but fallback search on raw is extra safety.
        def _find_url(text: str) -> str | None:
            for _pat in _URL_PATTERNS:
                m = _pat.search(text)
                if m:
                    candidate = m.group(0).rstrip(".,;)")
                    if "?" in candidate and (
                        "oauth" in candidate.lower()
                        or "client_id" in candidate.lower()
                        or "redirect_uri" in candidate.lower()
                    ):
                        return candidate
            return None

        oauth_url = _find_url(accumulated) or _find_url(accumulated_raw)
        if oauth_url:
            _log(f"PTY: OAuth URL captured: {oauth_url[:120]}...")
            global _active_oauth_url
            _active_oauth_url = oauth_url
            break

    if not oauth_url:
        rc = proc.poll()
        _log(f"PTY: FAILED to capture OAuth URL within deadline (process rc={rc}).")
        _log(f"PTY: Accumulated (cleaned, last 800): {accumulated[-800:]!r}")
        _log(f"PTY: Accumulated (raw, last 400): {accumulated_raw[-400:]!r}")
        return None, proc, master_fd  # caller will kill proc and close fd

    return oauth_url, proc, master_fd


def _do_auto_login(email: str) -> bool:
    """Core login flow — start CLI, capture URL, automate browser with n8n verification."""

    # Step 0: DELETE the expired credentials file before running claude login.
    #
    # CRITICAL: When an expired .credentials.json exists on disk, `claude login`
    # detects it, tries to re-authenticate using those stale credentials, gets a
    # 401 "Invalid authentication credentials" from the Anthropic API, and exits
    # with code 1 — WITHOUT ever printing an OAuth URL. The file MUST be removed
    # so the CLI has no existing auth state and is forced to start a fresh OAuth flow.
    _creds_mtime_before = None
    _creds_bak = _CREDS_FILE.with_name(".credentials.json.bak")
    if _CREDS_FILE.exists():
        try:
            _creds_mtime_before = _CREDS_FILE.stat().st_mtime
            # Rename to .bak — keeps a copy in case something goes wrong
            _CREDS_FILE.rename(_creds_bak)
            _log(f"Step 0: Moved expired credentials to .bak — forcing fresh OAuth flow (mtime={_creds_mtime_before:.0f}).")
        except Exception as _e0:
            _log(f"Step 0: Could not rename credentials (trying delete): {_e0}")
            try:
                _CREDS_FILE.unlink()
                _log("Step 0: Deleted expired credentials file.")
            except Exception as _e0b:
                _log(f"Step 0: WARNING — could not remove credentials file: {_e0b}. claude login may fail with 401.")

    # Step 1+2: Start `claude login` via PTY and capture the OAuth URL.
    #
    # CRITICAL: `claude login` only outputs an OAuth URL when it has a real TTY.
    # Without one (piped stdin/stdout), it detects the non-interactive environment
    # and tries a different auth path: "OAuth authentication is currently not supported."
    # We must run it with a pseudo-terminal (pty) so it thinks it has a real terminal.
    _log("Step 1: Starting `claude login` via pseudo-terminal (PTY)...")
    # Strip auth env vars so the CLI cannot fall back to any stored credentials
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDE_SESSION_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")}
    env["HOME"] = "/root"
    env["TERM"] = "xterm-256color"  # Ensure the CLI sees a proper terminal type

    oauth_url, proc, _pty_master_fd = _start_claude_login_pty(env)

    if not oauth_url:
        # _start_claude_login_pty already logged the failure details
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        if _pty_master_fd is not None:
            try:
                import os as _os
                _os.close(_pty_master_fd)
            except Exception:
                pass
        return False

    # Step 3: Trigger n8n email monitor BEFORE opening browser
    # n8n will start watching for the Anthropic verification email
    _log("Step 3a: Triggering n8n email monitor...")
    _trigger_n8n_email_monitor()

    # Show violet talking line: N8N Agent ↔ Claude CLI Pro
    # This is the communication channel during self-healing (n8n reads email → sends code)
    try:
        from .agent_status_tracker import mark_talking
        mark_talking("N8N Agent", "Claude CLI Pro")
    except Exception:
        pass

    # Step 3b: Automate the browser flow
    # Returns: (True, None)     = localhost callback, token saved by CLI automatically
    #          (True, "code...") = headless container mode, must write auth code to stdin
    #          (False, None)    = failure
    _log("Step 3b: Opening headless browser...")
    browser_ok, auth_code = _automate_browser(oauth_url, email)

    # Clear talking line once browser flow completes (success or failure)
    try:
        from .agent_status_tracker import clear_talking
        clear_talking("N8N Agent", "Claude CLI Pro")
    except Exception:
        pass

    if not browser_ok:
        # Browser automation failed (typically: datacenter IP blocked from sending magic link).
        # Don't kill the PTY yet — wait up to 10 minutes for the user to complete the OAuth
        # flow manually in their own browser and POST the auth code to /webhook/manual-auth-code.
        _log("Step 3 FAILED: Browser automation failed (likely datacenter IP block).")
        _log(f">>> MANUAL LOGIN REQUIRED <<<")
        _log(f">>> 1. Open this URL in your browser: {oauth_url}")
        _log(f">>> 2. Enter email → click magic link → copy the auth code")
        _log(f">>> 3. POST {{\"code\": \"...\"}} to /webhook/manual-auth-code")
        _log(f">>> Waiting {_MANUAL_AUTH_CODE_TIMEOUT // 60} minutes for manual auth code...")
        try:
            auth_code = _manual_auth_code_queue.get(timeout=_MANUAL_AUTH_CODE_TIMEOUT)
            _log(f"Manual auth code received: {auth_code[:20]}... — writing to PTY")
        except queue.Empty:
            _log("Manual auth code timeout — no code received within 10 minutes. Killing PTY.")
            proc.kill()
            return False

    # Step 4: Write auth code / confirmation to the PTY master fd.
    # With PTY mode proc.stdin is None — all writes go through the master fd.
    _log("Step 4: Waiting for CLI to complete login...")
    import os as _os4
    if auth_code:
        _log(f"Step 4: Container OAuth mode — writing auth code to PTY ({auth_code[:12]}...)...")
        try:
            # Raw mode: use \r (Enter) not \n
            _os4.write(_pty_master_fd, (auth_code + "\r").encode())
        except Exception as _e:
            _log(f"Step 4: Failed to write auth code to PTY master fd: {_e}")
    else:
        # Localhost callback mode — CLI already got its token via the browser redirect;
        # send \r (Enter in raw mode) in case it's waiting for the user to confirm.
        try:
            _os4.write(_pty_master_fd, b"\r")
        except Exception:
            pass

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _log("Step 4: CLI didn't exit in 30s — killing (token may still be saved).")
        proc.kill()

    # Close the PTY master fd now that the child has exited
    global _active_pty_master_fd
    try:
        _os4.close(_pty_master_fd)
        _pty_master_fd = None
    except Exception:
        pass
    _active_pty_master_fd = None  # clear global so /auth/login-status shows pty_active=false

    # Step 5: Verify the token was FRESHLY saved (not the old expired file)
    _log("Step 5: Verifying credentials...")
    time.sleep(2)  # Give CLI time to write the file

    if _CREDS_FILE.exists():
        # Confirm the file was actually written during THIS login attempt
        try:
            _creds_mtime_after = _CREDS_FILE.stat().st_mtime
            if _creds_mtime_before is not None and _creds_mtime_after <= _creds_mtime_before:
                _log(f"Step 5 FAILED: credentials file was NOT updated (mtime unchanged: {_creds_mtime_after:.0f}). "
                     "CLI likely did not complete the OAuth callback — the old expired token is still on disk.")
                return False
            _log(f"Step 5: credentials file updated (mtime {_creds_mtime_before} → {_creds_mtime_after:.0f}) ✓")
        except Exception:
            pass

        try:
            r = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True, text=True, timeout=15,
                env=env,
            )
            if '"authMethod": "claude.ai"' in r.stdout or '"authMethod":"claude.ai"' in r.stdout.replace(": ", ":"):
                _log("Step 5: LOGIN SUCCESS — Claude CLI Pro authenticated ✓")

                # Remove the .bak now that we have a fresh valid credentials file
                try:
                    if _creds_bak.exists():
                        _creds_bak.unlink()
                except Exception:
                    pass

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
    # Restore the .bak so the system isn't left with zero credentials on disk
    try:
        if _creds_bak.exists() and not _CREDS_FILE.exists():
            _creds_bak.rename(_CREDS_FILE)
            _log("Step 5: Restored .bak credentials (login failed — keeping old token).")
    except Exception:
        pass
    try:
        from .agent_status_tracker import mark_sick
        mark_sick("Claude CLI Pro")
    except Exception:
        pass
    return False


def _extract_oauth_code_from_page(page) -> str | None:
    """
    Extract the OAuth authorization code after being redirected to
    platform.claude.com/oauth/code/callback (container/headless mode).
    Returns the code string or None.
    """
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(page.url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            _log(f"Browser: extracted OAuth code from URL query param ({code[:12]}...)")
            return code
    except Exception:
        pass

    # Also try fragment (#code=...) — some OAuth servers use the fragment
    try:
        url = page.url
        if "#" in url:
            fragment = url.split("#", 1)[1]
            from urllib.parse import parse_qs
            fparams = parse_qs(fragment)
            code = fparams.get("code", [None])[0]
            if code:
                _log(f"Browser: extracted OAuth code from URL fragment ({code[:12]}...)")
                return code
    except Exception:
        pass

    # Try to extract from visible page text — the page shows something like:
    # "Your authorization code is: XXXXX" or "Copy this code: XXXXX"
    try:
        content = page.inner_text("body") if page else ""
        for pattern in [
            r"authorization[_ ]code[:\s]+([A-Za-z0-9\-_]{8,})",
            r"code[:\s]+([A-Za-z0-9\-_]{16,})",
            r'"code"\s*:\s*"([A-Za-z0-9\-_]{8,})"',
        ]:
            m = re.search(pattern, content, re.IGNORECASE)
            if m:
                code = m.group(1)
                _log(f"Browser: extracted OAuth code from page text ({code[:12]}...)")
                return code
    except Exception:
        pass

    return None


def _automate_browser(oauth_url: str, email: str) -> tuple[bool, str | None]:
    """
    Automate the Claude.ai passwordless OAuth login flow.

    Returns (success, auth_code) where:
      (True, None)    = localhost callback — CLI captured token automatically
      (True, "code")  = headless/container mode — write code to claude login stdin
      (False, None)   = failure

    Two redirect modes handled:
      LOCAL:     CLI starts localhost server; after magic link auth, browser redirects
                 to http://localhost:PORT/callback — CLI captures token automatically.
      CONTAINER: redirect_uri=https://platform.claude.com/oauth/code/callback;
                 that page displays an auth code the CLI reads from stdin.

    Browser priority:
      1. camoufox (patched Firefox) — passes Cloudflare Managed Challenge because its
         JA3 TLS fingerprint and browser internals are indistinguishable from real Firefox.
         Headless Chromium is blocked by CF even with JS stealth patches because CF
         detects it at the TLS fingerprint level.
      2. Playwright Chromium — fallback if camoufox is unavailable.
    """

    def _save_cookies(page) -> None:
        """Save browser session cookies for reuse on next auth attempt."""
        try:
            import json as _json
            _cookies = page.context.cookies()
            # Only save if we have claude.ai session cookies
            _claude_cookies = [c for c in _cookies if "claude" in c.get("domain", "")]
            if _claude_cookies:
                _COOKIES_FILE.write_text(_json.dumps(_cookies))
                _log(f"Browser: saved {len(_cookies)} cookies to {_COOKIES_FILE} "
                     f"({len(_claude_cookies)} claude.ai session cookies) ✓")
        except Exception as _ck_save_err:
            _log(f"Browser: cookie save failed (non-fatal): {_ck_save_err}")

    def _page_flow(page) -> tuple[bool, str | None]:
        """All page-level logic. Browser-agnostic — never closes the browser."""

        # ── Load saved session cookies ────────────────────────────────────
        # After a successful login, cookies are saved so that future sessions
        # can skip the email+magic-link flow entirely (session still active).
        _cookies_loaded = False
        try:
            import json as _json
            if _COOKIES_FILE.exists():
                _saved = _json.loads(_COOKIES_FILE.read_text())
                page.context.add_cookies(_saved)
                _cookies_loaded = True
                _log(f"Browser: loaded {len(_saved)} saved session cookies — "
                     "will skip email flow if session is still active")
        except Exception as _ck_load_err:
            _log(f"Browser: could not load saved cookies (non-fatal): {_ck_load_err}")

        # ── Navigate to OAuth URL ────────────────────────────────────────
        _log(f"Browser: navigating to OAuth URL: {oauth_url[:120]}...")
        page.goto(oauth_url, timeout=60000)

        # Wait for Cloudflare Managed Challenge to resolve.
        # Guard: document.title starts as "" before CF JS sets it, so require
        # non-empty title BEFORE checking that it's not a challenge title —
        # otherwise the check fires immediately as a false positive.
        _log("Browser: waiting for Cloudflare challenge to clear (if any)...")
        try:
            page.wait_for_function(
                "() => {"
                "  var t = document.title;"
                "  return t !== '' && !t.includes('moment') &&"
                "         !t.includes('Just a') && !t.includes('Checking') &&"
                "         !t.includes('challenge');"
                "}",
                timeout=45000,
                polling=1000,
            )
            _log("Browser: Cloudflare challenge cleared.")
        except Exception as _cf_e:
            _log(f"Browser: CF wait timed out ({_cf_e}) — proceeding anyway")
        time.sleep(1)
        _log(f"Browser: on page = {page.url[:120]}")
        _log(f"Browser: page title = {page.title()!r}")

        # ── Early callback (SSO / session already active) ────────────────
        if _is_callback_url(page.url):
            _log("Browser: already redirected to callback after navigation ✓")
            auth_code = (
                _extract_oauth_code_from_page(page)
                if "platform.claude.com" in page.url else None
            )
            _save_cookies(page)
            return True, auth_code

        # ── Accept cookie consent (if present) ──────────────────────────
        # Claude.ai shows a GDPR cookie banner that overlays the page.
        # It must be dismissed before interacting with the login form,
        # otherwise button clicks may hit the overlay instead of the form.
        try:
            _cookie_btn = page.query_selector('button:has-text("Accept All Cookies")')
            if _cookie_btn and _cookie_btn.is_visible():
                _cookie_btn.click()
                _log("Browser: accepted cookie consent overlay")
                time.sleep(1)
        except Exception as _ck_e:
            _log(f"Browser: cookie consent check skipped ({_ck_e})")

        # ── Session-active shortcut (cookies gave us an existing session) ─
        # If we loaded saved cookies and the OAuth page shows an existing-account
        # consent button (e.g. "Continue with your Claude.ai account"), click it
        # directly without going through the email/magic-link flow at all.
        # If the session is expired, the page will show a login form instead —
        # we delete the stale cookie file and fall through to the magic link flow.
        if _cookies_loaded:
            _log("Browser: checking for active session (saved cookies)...")
            _session_active = False
            try:
                # Wait briefly for React to render account-selection content
                time.sleep(2)
                _session_btn = None
                for _sbsel in [
                    'button:has-text("Continue with your")',
                    'button:has-text("Continue as")',
                    'button:has-text("Approve")',
                    'button:has-text("Allow")',
                    'button:has-text("Authorize")',
                ]:
                    _sb = page.query_selector(_sbsel)
                    if _sb and _sb.is_visible():
                        _session_btn = _sb
                        _log(f"Browser: active session detected — found {_sbsel!r}: {_sb.inner_text()!r}")
                        break

                if _session_btn:
                    _session_btn.click()
                    _log("Browser: clicked session-active button — waiting for callback...")
                    auth_code, ok = _wait_for_callback_and_extract(page)
                    if ok:
                        _save_cookies(page)
                        return ok, auth_code
                    # Button clicked but no callback — session was stale
                    _log("Browser: session-active click didn't lead to callback — "
                         "cookies are stale. Deleting cookie file and falling back to magic link.")
                else:
                    _log("Browser: saved cookies loaded but session has expired (no account button). "
                         "Deleting stale cookie file and falling back to magic link flow.")

            except Exception as _sess_e:
                _log(f"Browser: session-active check error: {_sess_e} — "
                     "falling back to magic link flow")

            # Session inactive / stale — delete cookie file so the next attempt
            # doesn't waste time trying them again before the session is refreshed.
            try:
                if _COOKIES_FILE.exists():
                    _COOKIES_FILE.unlink()
                    _log("Browser: stale cookie file deleted ✓")
            except Exception:
                pass
            # Fall through to the email / magic link flow below

        # ── Step 1: Enter email ──────────────────────────────────────────
        # Claude.ai is a React SPA — the email input is injected by JS after
        # the initial HTML loads. Must wait for it to appear in the DOM.
        _log("Browser: waiting for email input to render (React SPA)...")
        email_field = None
        _email_selectors = [
            'input[type="email"]',
            'input[placeholder*="email" i]',
            'input[name="email"]',
            'input[autocomplete*="email" i]',
            'input[autocomplete="username"]',
        ]
        for _sel in _email_selectors:
            try:
                page.wait_for_selector(_sel, timeout=20000)
                email_field = page.query_selector(_sel)
                if email_field:
                    _log(f"Browser: email input found (selector={_sel!r})")
                    break
            except Exception:
                pass

        if not email_field:
            _log("Browser: no email field appeared after 20s — page content for diagnosis:")
            _log(f"Browser: page content sample (for debugging): {page.content()[:600]}")
            _log(f"Browser: FULL page content: {page.content()[:2000]}")
            return False, None

        email_field.fill(email)
        _log(f"Browser: email entered ({email})")
        time.sleep(0.5)
        # Use precise selectors — "Continue with email" first, then exact
        # "Continue" text only (NOT "Continue with Google" / "Continue with SSO").
        # Query all matching buttons and pick the one whose visible text is exact.
        continue_btn = page.query_selector('button:has-text("Continue with email")')
        if not continue_btn:
            for _cb in page.query_selector_all('button[type="submit"], button:has-text("Continue")'):
                try:
                    _txt = _cb.inner_text().strip()
                    if _txt in ("Continue", "Continue with email") and _cb.is_visible():
                        continue_btn = _cb
                        break
                except Exception:
                    pass
        if continue_btn:
            continue_btn.click()
            _log("Browser: clicked 'Continue with email'")
        else:
            page.keyboard.press("Enter")
            _log("Browser: pressed Enter to submit email")
        time.sleep(3)
        _log(f"Browser: after email submit, URL = {page.url[:120]}")

        # ── Check post-submit ────────────────────────────────────────────
        if _is_callback_url(page.url):
            _log("Browser: callback after email submit ✓")
            auth_code = (
                _extract_oauth_code_from_page(page)
                if "platform.claude.com" in page.url else None
            )
            return True, auth_code

        # ── Handle selectAccount page (magic link not sent yet) ────────────
        # After submitting the email, claude.ai may redirect to:
        #   /login?selectAccount=true&returnTo=/oauth/authorize?...
        # This is an ACCOUNT SELECTION step — the user must click their email
        # address on this page to trigger Anthropic sending the magic link.
        # Until that click happens, no email is sent.
        if "selectAccount=true" in page.url:
            _log("Browser: on selectAccount page — waiting 3s for React to render...")
            time.sleep(3)

            # ALWAYS dump page text first so we can see exactly what's rendered
            try:
                _body_text = page.inner_text("body")
            except Exception:
                _body_text = "(could not read body)"
            _log(f"Browser: [selectAccount] page body text:\n{_body_text[:1200]}")

            # Also dump raw HTML so we can see element types + attributes
            try:
                _body_html = page.content()
            except Exception:
                _body_html = "(could not read html)"
            _log(f"Browser: [selectAccount] page HTML (first 2000 chars):\n{_body_html[:2000]}")

            # First: check if Anthropic returned an error (rate-limit / server error).
            # This shows up as "There was an error sending you a login link." in the body.
            try:
                _page_text_now = page.inner_text("body")
            except Exception:
                _page_text_now = ""
            if "error sending" in _page_text_now.lower() or "error sending you a login link" in _page_text_now.lower():
                _log("Browser: ERROR — Anthropic returned 'error sending login link'. "
                     "This is usually a rate-limit. Setting 1-hour cooldown.")
                _record_ratelimit_hit()
                return False, None

            _selected = False
            _email_local = email.split("@")[0]  # e.g. "gelson_m" from "gelson_m@hotmail.com"

            # Candidate selectors — email-specific ones first.
            # IMPORTANT: skip any button whose text contains "Google" or "SSO" or "different"
            # as those would restart the flow rather than selecting the existing account.
            _SKIP_TEXTS = ("google", "sso", "different", "reject", "customize", "cookie")

            def _safe_click_candidate(_asel: str) -> bool:
                """Try selector; return True if clicked a valid-looking element."""
                try:
                    _el = page.query_selector(_asel)
                    if not (_el and _el.is_visible()):
                        return False
                    _txt = (_el.inner_text() or "").strip()
                    if any(_s in _txt.lower() for _s in _SKIP_TEXTS):
                        _log(f"Browser: skipping selector={_asel!r} text={_txt!r} (excluded)")
                        return False
                    _log(f"Browser: found selector={_asel!r} text={_txt!r} — clicking")
                    _el.click()
                    return True
                except Exception as _e:
                    _log(f"Browser: selector {_asel!r} error: {_e}")
                    return False

            for _asel in [
                f'button:has-text("{email}")',
                f'[data-email="{email}"]',
                f'div[role="button"]:has-text("{email}")',
                f'li:has-text("{email}")',
                f'button:has-text("{_email_local}")',
                f'[class*="account"]:has-text("{email}")',
                f'[class*="account"]:has-text("{_email_local}")',
                # Exact "Continue" only — not "Continue with Google/SSO"
                'button[type="submit"]',
                'button:has-text("Sign in")',
            ]:
                if _safe_click_candidate(_asel):
                    _log(f"Browser: account selection clicked (selector={_asel!r})")
                    _selected = True
                    # Wait for URL change rather than sleeping blind
                    _prev_url = page.url
                    for _i in range(10):
                        time.sleep(1)
                        if page.url != _prev_url:
                            _log(f"Browser: URL changed after account select → {page.url[:120]}")
                            break
                    else:
                        _log(f"Browser: URL unchanged after 10s: {page.url[:120]}")
                        try:
                            _log(f"Browser: [selectAccount post-click] body:\n{page.inner_text('body')[:800]}")
                        except Exception:
                            pass
                    break

            if not _selected:
                _log("Browser: WARNING — could not find any clickable account button.")
                # Try pressing Enter on the page as a last resort
                try:
                    page.keyboard.press("Enter")
                    _log("Browser: pressed Enter on page as fallback")
                    time.sleep(3)
                    _log(f"Browser: URL after Enter: {page.url[:120]}")
                except Exception:
                    pass

        # Do NOT call _click_approve() here. After submitting the email and
        # selecting the account, Claude.ai shows a "check your email" page.
        # The real OAuth consent appears only AFTER the user navigates the magic link.

        # ── Step 2: Wait for magic link URL from n8n ────────────────────
        # Dump final state before waiting so we know what page we're on
        try:
            _final_pre_wait_text = page.inner_text("body")
            _log(f"Browser: [pre-magic-link-wait] URL={page.url[:120]}")
            _log(f"Browser: [pre-magic-link-wait] body:\n{_final_pre_wait_text[:800]}")
        except Exception:
            _final_pre_wait_text = ""

        # Abort early if Anthropic returned an error at this stage too
        if "error sending" in _final_pre_wait_text.lower():
            _log("Browser: ERROR — Anthropic returned 'error sending login link' before "
                 "magic link wait. Setting 1-hour cooldown.")
            try:
                _RATELIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
                _RATELIMIT_FILE.write_text(str(time.time() + _RATELIMIT_COOLDOWN))
            except Exception:
                pass
            return False, None
        _log("Browser: waiting for magic link URL from n8n email monitor...")
        magic_url = _wait_for_verification_code()
        if not magic_url:
            _log("Browser: TIMEOUT — n8n did not deliver the magic link URL within 3 minutes.")
            return False, None

        # ── Step 3: Navigate to the magic link ──────────────────────────
        _log(f"Browser: navigating to magic link: {magic_url[:100]}...")
        try:
            page.goto(magic_url, timeout=30000)
        except Exception as _nav_e:
            _log(f"Browser: magic link navigation warning (may be normal): {_nav_e}")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            time.sleep(3)
        _log(f"Browser: after magic link, URL = {page.url[:120]}")
        _log(f"Browser: page title = {page.title()!r}")
        _log(f"Browser: page content sample: {page.content()[:400]}")

        # ── Step 4: Handle post-magic-link state ────────────────────────
        if _is_callback_url(page.url):
            _log("Browser: callback after magic link navigation ✓")
            auth_code = (
                _extract_oauth_code_from_page(page)
                if "platform.claude.com" in page.url else None
            )
            _save_cookies(page)
            return True, auth_code

        _log("Browser: looking for Approve button on consent screen...")
        approved = _click_approve(page)
        if approved:
            _log("Browser: Approve clicked — waiting for callback redirect...")
            auth_code, ok = _wait_for_callback_and_extract(page)
            if ok:
                _save_cookies(page)
            return ok, auth_code

        _log(f"Browser: WARNING — no Approve button found. "
             f"Final URL: {page.url[:120]}. "
             f"Page content: {page.content()[:800]}")
        return False, None

    # ── Attempt 1: camoufox (patched Firefox — CF-bypass) ───────────────
    try:
        from camoufox.sync_api import Camoufox
        _log("Browser: using camoufox (patched Firefox — CF-bypass mode).")
        try:
            with Camoufox(headless=True, geoip=True) as browser:
                page = browser.new_page()
                return _page_flow(page)
        except Exception as _e:
            _log(f"Browser automation error (camoufox): {_e}")
            import traceback
            _log(f"Browser traceback: {traceback.format_exc()[:600]}")
            return False, None
    except ImportError:
        _log("Browser: camoufox not available — falling back to Playwright Chromium.")

    # ── Attempt 2: Playwright Chromium (fallback) ────────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log("Playwright not installed — cannot auto-login.")
        return False, None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,800",
                ]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
            )
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            """)
            page = context.new_page()
            result = _page_flow(page)
            browser.close()
            return result
    except Exception as e:
        _log(f"Browser automation error (playwright): {e}")
        import traceback
        _log(f"Browser traceback: {traceback.format_exc()[:600]}")
        return False, None


def _is_callback_url(url: str) -> bool:
    """Return True if the URL is an OAuth callback (localhost or platform.claude.com)."""
    return (
        "localhost" in url
        or "/callback" in url
        or "oauth/code/callback" in url
        or "platform.claude.com/oauth" in url
    )


def _wait_for_callback_and_extract(page) -> tuple[str | None, bool]:
    """
    Wait for the browser to land on a callback URL after Approve is clicked.
    Returns (auth_code, success).
      auth_code is None for localhost callbacks (CLI captures automatically),
      auth_code is the code string for platform.claude.com container-mode callbacks.
    """
    # Wait for either localhost OR platform.claude.com callback
    for pattern, mode in [
        ("*platform.claude.com/oauth*", "container"),
        ("*localhost*", "local"),
        ("*oauth/code/callback*", "container"),
    ]:
        try:
            page.wait_for_url(pattern, timeout=15000)
            cur_url = page.url
            _log(f"Browser: callback redirect ✓ mode={mode} URL={cur_url[:120]}")
            if mode == "container":
                code = _extract_oauth_code_from_page(page)
                return code, True
            else:
                return None, True
        except Exception:
            continue

    # Last resort: check current URL
    try:
        cur_url = page.url or ""
    except Exception:
        cur_url = ""

    if _is_callback_url(cur_url):
        _log(f"Browser: callback detected in current URL: {cur_url[:120]}")
        if "platform.claude.com" in cur_url:
            code = _extract_oauth_code_from_page(page)
            return code, True
        return None, True

    _log(f"Browser: no callback redirect detected. Final URL: {cur_url[:120]}")
    return None, False


def _click_approve(page) -> bool:
    """Find and click the OAuth Approve/Allow button."""
    try:
        # Check if we're already on a success/callback page before looking for buttons
        if "localhost" in page.url or "callback" in page.url:
            _log("Browser: already redirected to callback — auto-approved.")
            return True

        selectors = [
            # OAuth consent-specific buttons only — deliberately narrow to avoid
            # matching "Accept" / "Continue" / "Sign in" on confirmation pages.
            'button:has-text("Approve")',
            'button:has-text("Allow")',
            'button:has-text("Authorize")',
            'button:has-text("Allow access")',
            'button:has-text("Grant access")',
            'button:has-text("Grant")',
            'input[type="submit"][value*="Approve" i]',
            'input[type="submit"][value*="Allow" i]',
            'a:has-text("Approve")',
            'a:has-text("Allow")',
            '[data-testid*="approve" i]',
            '[data-testid*="allow" i]',
            '[data-testid*="authorize" i]',
            '[aria-label*="approve" i]',
            '[aria-label*="allow" i]',
        ]
        for sel in selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    _log(f"Browser: clicked '{sel}'")
                    return True
            except Exception:
                continue

        # Try wait_for_selector as last resort — button may still be loading
        for sel in ['button:has-text("Approve")', 'button:has-text("Allow")', 'button:has-text("Authorize")']:
            try:
                page.wait_for_selector(sel, timeout=3000)
                btn = page.query_selector(sel)
                if btn:
                    btn.click()
                    _log(f"Browser: clicked deferred '{sel}'")
                    return True
            except Exception:
                continue

        _log("Browser: no approve button found. Page content sample: " + page.content()[:400])
        return False

    except Exception as e:
        _log(f"Browser: approve click error — {e}")
        return False


def _push_token_to_railway() -> None:
    """
    Persist fresh credentials after a successful Playwright login.

    THREE-LAYER persistence (most reliable first):
      1. Volume backup  — write raw JSON to /workspace/.claude_credentials_backup.json
                          Survives container restarts without Railway API. Always works.
      2. Railway API    — update CLAUDE_SESSION_TOKEN env var via GraphQL (needs RAILWAY_TOKEN)
      3. Railway CLI    — fallback if API fails (also needs RAILWAY_TOKEN)

    Layer 1 alone is enough to survive restarts: entrypoint.sh reads the volume
    backup on boot before falling back to the env var.
    """
    try:
        if not _CREDS_FILE.exists():
            return
        raw = _CREDS_FILE.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")

        # ── Layer 0: update in-process env var immediately ────────────────────
        # This is the critical fix for the recovery loop: if CLAUDE_SESSION_TOKEN
        # env var is stale in the running process, `claude -p` will get 401 even
        # after the credentials file is refreshed.  Updating os.environ here means
        # the very next claude_pro subprocess picks up the fresh token without
        # needing a container restart.
        os.environ["CLAUDE_SESSION_TOKEN"] = encoded
        _log("In-process CLAUDE_SESSION_TOKEN updated ✓ (no restart needed)")

        # ── Layer 1: volume backup (always attempted, no Railway API needed) ────
        _VOLUME_BACKUP = Path("/workspace/.claude_credentials_backup.json")
        try:
            _VOLUME_BACKUP.write_bytes(raw)
            _VOLUME_BACKUP.chmod(0o600)
            _log(f"Token saved to volume backup ({_VOLUME_BACKUP}) ✓ — persists across restarts without Railway API.")
        except Exception as _ve:
            _log(f"Volume backup failed (workspace not writable?): {_ve}")

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

        # ── Push fresh token to super-agent in-memory via HTTP ────────────────
        # This is the critical bridge: Playwright runs on inspiring-cat, but
        # super-agent's local claude fallback uses its own /root/.claude/ which
        # still has the expired token.  Posting here updates super-agent's
        # in-memory env var + disk creds immediately — no redeploy required.
        _sa_url = os.environ.get("SUPER_AGENT_URL", "").rstrip("/")
        _api_key = os.environ.get("N8N_API_KEY", "") or os.environ.get("GITHUB_PAT", "")
        if _sa_url and _api_key:
            try:
                import urllib.request
                import json
                _refresh_payload = json.dumps({"token_b64": encoded, "api_key": _api_key}).encode()
                _req = urllib.request.Request(
                    f"{_sa_url}/webhook/refresh-cli-token",
                    data=_refresh_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(_req, timeout=10) as _resp:
                    _log(f"Fresh token pushed to super-agent in-memory ✓ (status {_resp.status})")
            except Exception as _pe:
                _log(f"Token push to super-agent failed (SUPER_AGENT_URL={_sa_url!r}): {_pe}")
        else:
            _log("SUPER_AGENT_URL or API key not set — skipping in-memory token push to super-agent. "
                 "Add SUPER_AGENT_URL=https://super-agent-production.up.railway.app to inspiring-cat Railway Variables.")

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

    FIXED: The real Anthropic OAuth server is at claude.com/cai/oauth/ —
    confirmed from captured OAuth URLs (authorize endpoint is
    https://claude.com/cai/oauth/authorize). Old guesses (claude.ai/api/oauth,
    api.anthropic.com/oauth) always returned 404 or 405, causing this layer
    to silently fail and fall through to the full browser flow every time.
    """
    try:
        import json
        import urllib.request
        import urllib.parse

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
            for _nested_key in ("oauth", "claudeAiOAuth", "session", "credentials"):
                _nested = creds.get(_nested_key, {})
                if isinstance(_nested, dict):
                    refresh_token = (
                        _nested.get("refreshToken")
                        or _nested.get("refresh_token")
                        or _nested.get("oauthRefreshToken")
                    )
                    if refresh_token:
                        break

        if not refresh_token:
            _log("Direct refresh: no refresh_token found in credentials file. "
                 f"Top-level keys: {list(creds.keys())}")
            return False

        _log(f"Direct refresh: found refresh_token (len={len(refresh_token)}) — attempting OAuth refresh...")

        # The confirmed Anthropic OAuth client_id (from captured OAuth URL):
        # client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e
        # Fall back to what's in credentials, then hardcoded known value.
        _KNOWN_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
        client_id = (
            creds.get("clientId")
            or creds.get("client_id")
            or creds.get("oauth", {}).get("clientId")
            or _KNOWN_CLIENT_ID
        )

        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }).encode()

        # Endpoints ordered by likelihood — claude.com/cai/oauth/token is the
        # confirmed real OAuth server (we see /cai/oauth/authorize in live URLs).
        # The previous list only contained guesses that always returned 404/405.
        endpoints = [
            "https://claude.com/cai/oauth/token",
            "https://claude.ai/cai/oauth/token",
            "https://api.claude.ai/oauth/token",
            "https://claude.ai/api/oauth/token",
            "https://api.anthropic.com/oauth/token",
        ]

        for endpoint in endpoints:
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "claude-cli/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode())
                    new_access = result.get("access_token")
                    new_refresh = result.get("refresh_token", refresh_token)

                    if new_access:
                        _log(f"Direct refresh SUCCESS via {endpoint} ✓")
                        # Update all known credential key patterns so whatever
                        # the CLI version expects is updated.
                        for _top_key in ("accessToken", "access_token"):
                            if _top_key in creds:
                                creds[_top_key] = new_access
                        for _top_key in ("refreshToken", "refresh_token", "oauthRefreshToken"):
                            if _top_key in creds:
                                creds[_top_key] = new_refresh
                        for _nested_key in ("oauth", "claudeAiOAuth", "session", "credentials"):
                            _nested = creds.get(_nested_key, {})
                            if isinstance(_nested, dict):
                                for _ak in ("accessToken", "access_token"):
                                    if _ak in _nested:
                                        _nested[_ak] = new_access
                                for _rk in ("refreshToken", "refresh_token"):
                                    if _rk in _nested:
                                        _nested[_rk] = new_refresh
                                # Also update expiresAt if returned
                                _new_exp = result.get("expires_in")
                                if _new_exp:
                                    import time as _t
                                    _exp_ts = int((_t.time() + _new_exp) * 1000)
                                    for _ek in ("expiresAt", "expires_at"):
                                        if _ek in _nested:
                                            _nested[_ek] = _exp_ts
                                creds[_nested_key] = _nested

                        _CREDS_FILE.write_text(json.dumps(creds, indent=2))
                        _CREDS_FILE.chmod(0o600)
                        _push_token_to_railway()
                        return True
                    else:
                        _log(f"Direct refresh: {endpoint} returned 200 but no access_token — "
                             f"response keys: {list(result.keys())}")

            except urllib.error.HTTPError as e:
                # Log the full response body — critical for diagnosing wrong endpoint
                try:
                    _body = e.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    _body = "(unreadable)"
                _log(f"Direct refresh: {endpoint} returned HTTP {e.code} — body: {_body}")
                continue
            except Exception as e:
                _log(f"Direct refresh: {endpoint} error — {e}")
                continue

        _log("Direct refresh: all OAuth endpoints failed.")
        return False

    except Exception as e:
        _log(f"Direct refresh error: {e}")
        return False


def maybe_proactive_refresh() -> bool:
    """
    Proactively refresh the OAuth access_token before it expires.

    Called by the watchdog scheduler even when the CLI is NOT down, so tokens
    are silently rotated before any request fails.  Returns True if a refresh
    was performed (or was unnecessary), False only on hard failure.

    Strategy:
      - Read expiresAt from credentials (milliseconds epoch, as stored by Claude CLI)
      - If more than 2 hours remain → do nothing (still fresh)
      - If < 2 hours remain → call _try_direct_refresh() to get a new token
      - If no expiresAt → attempt refresh anyway (credentials format unknown,
        better safe than sorry)
    """
    try:
        import json
        import time as _time

        if not _CREDS_FILE.exists():
            return False

        creds = json.loads(_CREDS_FILE.read_text())

        # Extract expiresAt — milliseconds epoch (Claude CLI stores ms, not seconds)
        expires_at_ms = None
        for _key in ("expiresAt", "expires_at"):
            if _key in creds:
                expires_at_ms = creds[_key]
                break
        if expires_at_ms is None:
            for _nested_key in ("oauth", "claudeAiOAuth", "session"):
                _nested = creds.get(_nested_key, {})
                if isinstance(_nested, dict):
                    for _key in ("expiresAt", "expires_at"):
                        if _key in _nested:
                            expires_at_ms = _nested[_key]
                            break
                if expires_at_ms is not None:
                    break

        now_ms = _time.time() * 1000

        if expires_at_ms is not None:
            remaining_s = (expires_at_ms - now_ms) / 1000
            if remaining_s > 7200:  # more than 2 hours — nothing to do
                _log(f"Proactive refresh: token still fresh ({int(remaining_s // 3600)}h "
                     f"{int((remaining_s % 3600) // 60)}m remaining) — skipping.")
                return True
            _log(f"Proactive refresh: token expires in {int(remaining_s // 60)}m — refreshing now...")
        else:
            _log("Proactive refresh: expiresAt not found in credentials — attempting refresh anyway.")

        # Don't run if recovery is already in progress
        if _recovery_running.is_set():
            _log("Proactive refresh: recovery already in progress — skipping.")
            return True

        return _try_direct_refresh()

    except Exception as _e:
        _log(f"Proactive refresh error: {_e}")
        return False


def full_recovery_chain() -> bool:
    """
    Complete recovery chain — try everything in order:
      1. Direct OAuth refresh (lightweight, no browser)
      2. Full browser auto-login (Playwright — nuclear option)

    NOTE: Env-var restore (previously step 2) is intentionally OMITTED here.
    This function is only called after _try_restore_claude_auth() has already
    been exhausted N times by pro_router — repeating it here would just write
    the same expired token to disk again and short-circuit before Playwright runs.

    Returns True if ANY method succeeded.

    CONCURRENCY GUARD: This function uses _recovery_lock to ensure only ONE
    recovery chain runs at a time. The watchdog, pro_router, and token_keeper
    all call this function simultaneously when the token expires — without the
    guard they each spawn a browser and submit the email within seconds of each
    other, triggering Anthropic's "error sending login link" rate-limit.
    Callers that lose the lock race block until the winner finishes, then return
    True if the winner already restored the token.
    """
    global _recovery_started_at

    # ── Rate-limit guard ────────────────────────────────────────────────────
    # If a previous attempt hit Anthropic's "error sending login link", we
    # back off with escalating cooldown (1h → 4h → 24h) before retrying.
    _remaining = _check_ratelimit()
    if _remaining > 0:
        _log(f"=== Login rate-limited — {int(_remaining // 60)}m {int(_remaining % 60)}s remaining. "
             f"Skipping recovery until cooldown expires. ===")
        return False

    # ── Concurrency guard ───────────────────────────────────────────────────
    # Only ONE recovery chain runs at a time. Others block and inherit the result.
    def _winner_succeeded(since: float) -> bool:
        """True if credentials file was freshly written AFTER `since`."""
        try:
            return _CREDS_FILE.exists() and _CREDS_FILE.stat().st_mtime > since
        except Exception:
            return False

    if _recovery_running.is_set():
        _log("=== Recovery already in progress — waiting for it to complete ===")
        _t = time.time()
        _recovery_running.wait(timeout=300)
        if _winner_succeeded(_t):
            _log("=== Recovery completed by another thread — fresh credentials detected ✓ ===")
            return True
        _log("=== Recovery completed by another thread — credentials NOT refreshed ===")
        return False

    if not _recovery_lock.acquire(blocking=False):
        _log("=== Recovery lock busy — waiting for existing recovery to finish ===")
        _t = time.time()
        _recovery_running.wait(timeout=300)
        if _winner_succeeded(_t):
            return True
        return False

    _recovery_started_at = time.time()
    _recovery_running.set()
    try:
        _log("=== Starting full CLI recovery chain ===")

        # Attempt 1: Direct OAuth refresh (no browser, uses refresh_token)
        _log("Recovery attempt 1/2: Direct OAuth refresh...")
        if _try_direct_refresh():
            _log("=== Recovery SUCCESS via direct OAuth refresh ===")
            return True

        # Attempt 2: Full browser auto-login (Playwright + n8n email monitor)
        _log("Recovery attempt 2/2: Full browser auto-login via Playwright...")
        if auto_login_claude():
            _log("=== Recovery SUCCESS via browser auto-login (Playwright) ===")
            return True

        _log("=== ALL recovery methods FAILED — manual login required ===")
        return False
    finally:
        _recovery_running.clear()
        _recovery_lock.release()
