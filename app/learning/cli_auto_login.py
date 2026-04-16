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

# Playwright timeout backoff — prevents rapid retry loops that exhaust Anthropic's
# magic link email quota (Anthropic silently stops sending emails after 2-3 rapid
# requests; no "error sending login link" is returned so _check_ratelimit() doesn't
# trigger). Set whenever the browser attempt times out; cleared after 20 minutes.
_last_playwright_timeout: float = 0.0
_PLAYWRIGHT_BACKOFF_S: float = 20 * 60  # 20 minutes between Playwright attempts
# Timestamps of every Playwright browser-login attempt (unix epoch, float).
# Used to detect thrashing: alert if > 3 attempts within a 1-hour window.
_playwright_attempts_log: list = []


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
_VERIFICATION_CODE_TIMEOUT = 360  # max seconds to wait for magic URL from n8n
# 360s = 6 minutes: email delivery (~15s) + n8n poll interval (up to 60s) + POST + buffer.
# Increased from 180s: Hotmail delivery can be delayed by up to 3 min from Railway IPs
# and the browser flow itself takes ~30s before we start waiting, giving n8n just 150s
# with the old value (only 1-2 poll cycles in the worst case).

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


def _drain_verification_queue() -> None:
    """
    Discard any stale magic link URLs that arrived during a PREVIOUS timed-out
    attempt.  Must be called at the start of each new login attempt.

    Why: if attempt N times out after 360s, a URL delivered by n8n at T+361s
    sits in the queue.  Attempt N+1 then calls _wait_for_verification_code()
    and immediately consumes the STALE URL from attempt N — that URL is
    single-use and already expired, so the navigation fails silently.
    """
    drained = 0
    while True:
        try:
            _verification_code_queue.get_nowait()
            drained += 1
        except queue.Empty:
            break
    if drained:
        _log(f"Drained {drained} stale magic link URL(s) from previous attempt(s).")


def _wait_for_verification_code() -> str | None:
    """Block until magic link URL arrives from n8n, or timeout. Returns oldest URL."""
    _log(f"Waiting for magic link URL from n8n (timeout: {_VERIFICATION_CODE_TIMEOUT}s)...")
    try:
        code = _verification_code_queue.get(timeout=_VERIFICATION_CODE_TIMEOUT)
        return code.strip()
    except queue.Empty:
        _log("Magic link TIMEOUT — n8n did not send the URL in time.")
        return None


def _collect_magic_links() -> list:
    """
    Wait for the first magic link URL, then collect any extras that arrive
    within 3 seconds (n8n sends all unread emails in rapid succession).

    Returns a list of URLs ordered oldest-first (so we try newest last).
    The NEWEST magic link is the valid one — old ones were already consumed
    by previous browser sessions and return "unable to verify".

    Why reverse order matters: the Hotmail inbox accumulates many unread
    Anthropic emails from repeated failed attempts. n8n finds ALL unread
    emails and sends them all. The oldest emails have stale one-time magic
    links. The most recently received email has the fresh one.
    """
    urls = []
    # Wait for first URL (up to full timeout)
    try:
        first = _verification_code_queue.get(timeout=_VERIFICATION_CODE_TIMEOUT)
        urls.append(first.strip())
        _log(f"Collected magic link 1: {first[:60]}...")
    except queue.Empty:
        _log("Magic link TIMEOUT — n8n did not send the URL in time.")
        return []

    # Drain any additional URLs that arrive quickly
    _extra_deadline = time.time() + 3.0
    while time.time() < _extra_deadline:
        try:
            extra = _verification_code_queue.get_nowait()
            urls.append(extra.strip())
            _log(f"Collected magic link {len(urls)}: {extra[:60]}...")
        except queue.Empty:
            time.sleep(0.2)

    _log(f"Total magic link URL(s) collected: {len(urls)} — will try newest first")
    return urls


def _trigger_n8n_email_monitor() -> bool:
    """
    The Claude-Verification-Monitor n8n workflow (ID: jun8CaMnNhux1iEY) polls
    the Hotmail inbox on a schedule and POSTs magic link URLs to
    /webhook/verification-code on inspiring-cat automatically.

    It has an Outlook trigger node — there is NO inbound HTTP trigger webhook.
    This function logs that we are waiting and also checks the workflow's active
    status via the n8n API (when N8N_BASE_URL and N8N_API_KEY are set) so we
    can diagnose failures faster.
    """
    _log("n8n Claude-Verification-Monitor will poll Hotmail automatically "
         f"and POST the magic link to /webhook/verification-code. "
         f"Waiting up to {_VERIFICATION_CODE_TIMEOUT}s...")

    # Quick n8n API check — verify the workflow is active and not erroring.
    # Non-fatal: we proceed even if this check fails.
    _N8N_WORKFLOW_ID = "jun8CaMnNhux1iEY"
    try:
        import urllib.request, json as _json
        n8n_url = os.environ.get("N8N_BASE_URL", "").rstrip("/")
        n8n_key = os.environ.get("N8N_API_KEY", "")
        if n8n_url and n8n_key:
            req = urllib.request.Request(
                f"{n8n_url}/api/v1/workflows/{_N8N_WORKFLOW_ID}",
                headers={"X-N8N-API-KEY": n8n_key, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                wf = _json.loads(resp.read().decode())
                active = wf.get("active", False)
                _log(f"n8n workflow '{wf.get('name', _N8N_WORKFLOW_ID)}' active={active}")
                if not active:
                    _log("WARNING: Claude-Verification-Monitor workflow is INACTIVE in n8n! "
                         "Magic link emails will NOT be delivered. "
                         "Activate it at your n8n instance → Workflows → Claude Verification Code Monitor → toggle Active.")
        else:
            _log("n8n status check skipped (N8N_BASE_URL or N8N_API_KEY not set).")
    except Exception as _n8n_e:
        _log(f"n8n workflow status check failed (non-fatal): {_n8n_e}")

    return True


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
    _fired_markers: set[str] = set()  # prevent same marker from firing Enter twice

    def _maybe_respond(text_no_spaces: str) -> bool:
        """Send Enter for the first matching prompt. Returns True if sent."""
        nonlocal _last_response_time
        now = time.time()
        if now - _last_response_time < _RESPONSE_COOLDOWN:
            return False  # still in cooldown — try again next cycle
        for marker, response in _ONBOARDING_RESPONSES:
            if marker in text_no_spaces and marker not in _fired_markers:
                _fired_markers.add(marker)
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

    # Drain any stale magic link URLs from previous failed/timed-out attempts.
    # This prevents a late-arriving URL from attempt N from being consumed by
    # attempt N+1 as if it were a fresh URL (see _drain_verification_queue docs).
    _drain_verification_queue()

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
    #
    # PTY BUFFER DEADLOCK FIX:
    # The browser automation takes ~2 minutes. During this time nobody is reading
    # from _pty_master_fd. The Claude CLI keeps printing output (spinner, prompts,
    # "Paste authorization code:" etc.) into the PTY kernel buffer. The default
    # PTY buffer is 4096 bytes. Once full, the CLI's write() call blocks and the
    # CLI freezes. When we then write the auth code, the CLI can't read it because
    # it's stuck trying to write — a classic PTY deadlock.
    #
    # Solution: drain the PTY output buffer BEFORE writing the code (unblocking any
    # pending CLI write), write the code, then keep draining while waiting for the
    # CLI to process it (to prevent re-deadlock).
    _log("Step 4: Waiting for CLI to complete login...")
    import os as _os4
    import select as _sel4

    def _drain_pty_output(fd: int, max_reads: int = 200, label: str = "") -> str:
        """Read and return all pending output from PTY master fd (non-blocking)."""
        _buf = []
        for _ in range(max_reads):
            try:
                r, _, _ = _sel4.select([fd], [], [], 0.05)
                if not r:
                    break
                _chunk = _os4.read(fd, 4096)
                if _chunk:
                    _buf.append(_chunk.decode("utf-8", errors="replace"))
            except Exception:
                break
        result = "".join(_buf)
        if result.strip() and label:
            _log(f"PTY {label} ({len(result)} bytes): {result.strip()[-300:]!r}")
        return result

    # Drain accumulated output to unblock the CLI before writing the code
    _drained = _drain_pty_output(_pty_master_fd, label="pre-code drain")

    if auth_code:
        _log(f"Step 4: Container OAuth mode — writing auth code to PTY ({auth_code[:12]}...)...")
        try:
            # Raw mode: use \r (Enter) not \n
            _os4.write(_pty_master_fd, (auth_code + "\r").encode())
            _log("Step 4: Auth code written to PTY ✓")
        except Exception as _e:
            _log(f"Step 4: Failed to write auth code to PTY master fd: {_e}")
    else:
        # Localhost callback mode — CLI already got its token via the browser redirect;
        # send \r (Enter in raw mode) in case it's waiting for the user to confirm.
        try:
            _os4.write(_pty_master_fd, b"\r")
        except Exception:
            pass

    # Keep draining PTY output while waiting for the CLI to finish.
    # CRITICAL: After receiving the auth code, the CLI may show additional
    # post-login prompts that require Enter:
    #   - "Authentication successful! Press any key to continue."
    #   - Analytics/feedback opt-in ("Would you like to share usage data? Y/n")
    #   - "Your conversation history will be stored locally. Press Enter..."
    # These come AFTER the oauth URL reading loop exits, so the onboarding
    # handler never sees them. We must send Enter periodically during the drain.
    _log("Step 4: Draining PTY output while CLI processes code (up to 120s)...")
    _code_sent_at = time.time()
    _deadline_step4 = time.time() + 120
    _last_enter_time = 0.0
    _ENTER_INTERVAL = 5.0  # send Enter every 5s to dismiss any post-auth prompts

    while time.time() < _deadline_step4:
        output = _drain_pty_output(_pty_master_fd, max_reads=10, label="post-code")
        if proc.poll() is not None:
            _log(f"Step 4: CLI exited (rc={proc.poll()}) ✓")
            break
        # Short-circuit: if credentials file was written AFTER we sent the auth code,
        # auth is complete. Kill the process immediately — prevents sending Enter into
        # the interactive Claude REPL that `claude login` transitions into post-auth.
        try:
            if _CREDS_FILE.exists() and _CREDS_FILE.stat().st_mtime > _code_sent_at:
                _log("Step 4: Credentials file updated — auth complete, terminating CLI.")
                proc.terminate()
                time.sleep(1.5)
                if proc.poll() is None:
                    proc.kill()
                break
        except Exception:
            pass
        # Periodically send Enter to dismiss any remaining post-auth prompts
        _now = time.time()
        if _now - _last_enter_time >= _ENTER_INTERVAL:
            try:
                _os4.write(_pty_master_fd, b"\r")
                _last_enter_time = _now
                if output.strip():
                    _log("Step 4: Sent Enter to dismiss post-auth prompt")
            except Exception:
                pass
        time.sleep(0.5)
    else:
        _log("Step 4: CLI didn't exit in 120s — killing (token may still be saved).")
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

                # Log the credential structure so we can see what keys are present
                # for diagnosing _try_direct_refresh() refresh_token discovery.
                try:
                    import json as _json_dbg
                    _creds_dbg = _json_dbg.loads(_CREDS_FILE.read_text())
                    _top_keys = list(_creds_dbg.keys())
                    _nested_keys = {k: list(v.keys()) for k, v in _creds_dbg.items()
                                    if isinstance(v, dict)}
                    _log(f"Step 5: Credentials top-level keys: {_top_keys}")
                    if _nested_keys:
                        _log(f"Step 5: Credentials nested keys: {_nested_keys}")
                    # Specifically check for refresh token presence
                    _has_rt = any("refresh" in str(k).lower() for k in _top_keys)
                    if not _has_rt:
                        for _nk, _nv in _nested_keys.items():
                            if any("refresh" in str(k).lower() for k in _nv):
                                _has_rt = True
                                break
                    _log(f"Step 5: refresh_token present in credentials: {_has_rt}")
                except Exception as _dbg_e:
                    _log(f"Step 5: Could not inspect credentials structure: {_dbg_e}")

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

    IMPORTANT: platform.claude.com exchanges the OAuth code server-side when the
    browser navigates to the callback URL, then DISPLAYS a code on the page.
    We must read the RENDERED PAGE TEXT — the URL query param code may already
    be consumed by the server and rejected by the CLI if we try to paste it.
    Page text extraction is always attempted first.

    Returns the code string or None.
    """
    import time as _t_ec

    # ── Step 1: Wait for React SPA to render the code ──────────────────────
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    _t_ec.sleep(2)

    # ── Step 2: Log the full page text (critical for debugging) ────────────
    _page_text = ""
    try:
        _page_text = page.inner_text("body") or ""
        _log(f"Browser: callback page rendered text:\n{_page_text[:1200]}")
    except Exception as _e:
        _log(f"Browser: could not read callback page text: {_e}")

    # ── Step 3: Extract from visible page text FIRST ───────────────────────
    # The platform.claude.com callback page shows something like:
    # "Copy this code: XXXXX" / "Paste this code into your terminal: XXXXX"
    # This is the code the CLI is waiting for at "Paste code here if prompted >"
    if _page_text:
        try:
            for pattern in [
                # "Paste this into Claude Code:\nCODE#STATE" — exact platform.claude.com format
                # The full code is CODE#STATE where # is a literal separator.
                r"Paste this into Claude Code[:\s]*[\r\n]+\s*([A-Za-z0-9\-_+/=#]{20,})",
                r"paste this into claude code[:\s]*[\r\n]+\s*([A-Za-z0-9\-_+/=#]{20,})",
                # Generic "copy/paste this code:" patterns — include # in char class
                r"copy this code[:\s]+([A-Za-z0-9\-_+/=#]{8,})",
                r"paste this code[:\s]+([A-Za-z0-9\-_+/=#]{8,})",
                r"authorization[_ ]code[:\s]+([A-Za-z0-9\-_+/=#]{8,})",
                r"your code[:\s]+([A-Za-z0-9\-_+/=#]{8,})",
                r"code[:\s]+([A-Za-z0-9\-_+/=#]{40,})",  # Long OAuth-style codes (incl. #state)
                r'"code"\s*:\s*"([A-Za-z0-9\-_+/=#]{8,})"',
            ]:
                m = re.search(pattern, _page_text, re.IGNORECASE)
                if m:
                    code = m.group(1).strip()
                    _log(f"Browser: extracted display code from page text ({code[:20]}...)")
                    return code
        except Exception:
            pass

    # ── Step 4: Try HTML elements (pre, code, input[value]) ────────────────
    try:
        _html = page.content()
        for html_pattern in [
            r'<(?:pre|code)[^>]*>([A-Za-z0-9\-_+/=#]{20,}[#][A-Za-z0-9\-_+/=]+)</(?:pre|code)>',  # code#state
            r'<(?:pre|code)[^>]*>([A-Za-z0-9\-_+/=]{20,})</(?:pre|code)>',
            r'<input[^>]+value="([A-Za-z0-9\-_+/=#]{20,})"',
        ]:
            m = re.search(html_pattern, _html)
            if m:
                code = m.group(1)
                _log(f"Browser: extracted code from HTML element ({code[:12]}...)")
                return code
    except Exception:
        pass

    # ── Step 5: Fall back to URL query param ───────────────────────────────
    # WARNING: this code may already be consumed by the server when the browser
    # navigated to the callback URL. Only used if page text extraction fails.
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(page.url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            _log(f"Browser: falling back to URL query param code ({code[:12]}...) — WARNING: may be consumed by server")
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

    _log("Browser: WARNING — could not extract any code from callback page. Returning None.")
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
            _ERR_PHRASES = (
                "error sending",                       # English
                "error sending you a login link",      # English exact
                "une erreur s'est produite",           # French
                "er is een fout opgetreden",           # Dutch
                "ein fehler ist aufgetreten",          # German
                "se ha producido un error",            # Spanish
            )
            if any(p in _page_text_now.lower() for p in _ERR_PHRASES):
                _log("Browser: ERROR — Anthropic returned 'error sending login link' "
                     f"(detected in page text). This is usually a rate-limit. Setting cooldown.")
                _record_ratelimit_hit()
                return False, None

            # ── Early exit: already in verification-code / magic-link mode ──────
            # When Claude.ai sends the magic link email ON the "Continue with email"
            # click (rather than after account selection), the selectAccount page
            # immediately shows a code/link entry form — NOT an account picker.
            # Indicators: "Enter the verification code", "Verify Email Address" button.
            # In this state we must NOT click any button (doing so submits an empty
            # code form → "There was an error verifying your code" + wasted attempt).
            # Instead, fall straight through to the magic link wait below.
            _in_verification_mode = (
                "enter the verification code" in _page_text_now.lower()
                or "verification code sent to" in _page_text_now.lower()
                or "verify email address" in _page_text_now.lower()
                or "check your email" in _page_text_now.lower()
            )
            if _in_verification_mode:
                _log("Browser: page is already in verification-code / magic-link mode "
                     "(Anthropic sent the email on 'Continue with email' click). "
                     "Skipping account-selection click — proceeding directly to magic link wait.")
                # Don't click anything. Fall through to _wait_for_verification_code() below.
            else:
                _selected = False
                _email_local = email.split("@")[0]  # e.g. "gelson_m" from "gelson_m@hotmail.com"

                # Candidate selectors — email-specific ones first.
                # IMPORTANT: skip any button whose text contains "Google" or "SSO" or "different"
                # as those would restart the flow rather than selecting the existing account.
                # ALSO skip "Verify Email Address" — that's a code-submission button, not account-select.
                _SKIP_TEXTS = ("google", "sso", "different", "reject", "customize", "cookie",
                               "verify email", "verification")

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

                # Note: 'button[type="submit"]' is intentionally removed — it's too broad
                # and matches the "Verify Email Address" code-submission button when the
                # page transitions to verification mode mid-render.
                for _asel in [
                    f'button:has-text("{email}")',
                    f'[data-email="{email}"]',
                    f'div[role="button"]:has-text("{email}")',
                    f'li:has-text("{email}")',
                    f'button:has-text("{_email_local}")',
                    f'[class*="account"]:has-text("{email}")',
                    f'[class*="account"]:has-text("{_email_local}")',
                    'button:has-text("Sign in")',
                    'button:has-text("Continue")',
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
                    _log("Browser: WARNING — could not find any clickable account button. "
                         "The page may have transitioned to verification-code mode mid-render. "
                         "Proceeding to magic link wait without clicking.")

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
        _ERR_PHRASES_PREFLIGHT = (
            "error sending",
            "error sending you a login link",
            "une erreur s'est produite",
            "er is een fout opgetreden",
            "ein fehler ist aufgetreten",
            "se ha producido un error",
        )
        if any(p in _final_pre_wait_text.lower() for p in _ERR_PHRASES_PREFLIGHT):
            _log("Browser: ERROR — Anthropic returned 'error sending login link' before "
                 "magic link wait (detected in page text). Setting cooldown via _record_ratelimit_hit().")
            _record_ratelimit_hit()
            return False, None
        _log("Browser: waiting for magic link URL(s) or verification code from n8n email monitor...")
        magic_urls = _collect_magic_links()
        if not magic_urls:
            _log("Browser: TIMEOUT — n8n did not deliver any magic link URL.")
            return False, None

        # ── Step 3: Detect auth flow variant ────────────────────────────────
        # New Anthropic flow (2026+): magic link page now shows a 6-digit
        # "Use verification code: XXXXXX" UI. That code must be entered on
        # the ORIGINAL OAuth page (the tab this browser is already on).
        # Detection: look for a numeric code-entry input on the current page.
        # Legacy flow: magic link navigation goes straight to callback URL or
        # OAuth consent screen — no code input on this page.
        import re as _re2
        _verification_input = None
        for _vsel in [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name="code"]',
            'input[type="text"][maxlength="6"]',
            'input[placeholder*="code" i]',
            'input[placeholder*="verification" i]',
        ]:
            try:
                _vi = page.query_selector(_vsel)
                if _vi and _vi.is_visible():
                    _verification_input = _vi
                    _log(f"Browser: verification code input detected (selector={_vsel!r}) — new auth flow")
                    break
            except Exception:
                pass

        if _verification_input:
            # ── New flow: extract 6-digit code then enter on THIS page ──────
            # The magic link URL, when navigated, shows the code on the page.
            # Open it in a new tab so we don't lose the original OAuth session.
            six_digit_code = None
            for magic_url in reversed(magic_urls):
                # Case A: n8n/user already extracted the code and posted it directly
                _stripped = magic_url.strip()
                if _stripped.isdigit() and 4 <= len(_stripped) <= 8:
                    six_digit_code = _stripped
                    _log(f"Browser: received direct numeric verification code from queue: {six_digit_code}")
                    break
                # Case B: URL was posted — open it as a popup in the SAME browser context.
                # Using page.expect_popup() + window.open() shares cookies and TLS fingerprint
                # with the main page, bypassing Cloudflare bot detection.
                # The magic link may behave in two ways:
                #  (a) Complete auth inline → consent page → callback URL (new 2026 flow)
                #  (b) Show a 6-digit code that must be entered on the original page (alt flow)
                _log(f"Browser: opening magic link as popup (same context): {magic_url[:80]}...")
                _popup = None
                _popup_auth_result = None   # (auth_code, ok) if popup completes auth itself
                try:
                    # Open about:blank popup in the same context (shares all cookies/CF clearance)
                    with page.expect_popup(timeout=35000) as _popup_info:
                        page.evaluate("window.open('about:blank', '_blank')")
                    _popup = _popup_info.value
                    # Explicitly navigate so Playwright handles the # fragment and load states
                    _popup.goto(magic_url, timeout=30000)
                    try:
                        _popup.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    # Brief wait for JavaScript to render content after networkidle
                    time.sleep(2)
                    _tab_url = ""
                    _tab_body = ""
                    try:
                        _tab_url = _popup.url
                        _tab_body = _popup.inner_text("body") or ""
                    except Exception:
                        pass
                    _log(f"Browser: [magic-link-popup] url={_tab_url[:120]}")
                    _log(f"Browser: [magic-link-popup] body (first 400): {_tab_body[:400]}")

                    # Path A: popup already landed on callback URL (direct auth)
                    if _is_callback_url(_tab_url):
                        _log("Browser: popup completed auth directly → callback URL")
                        _popup_auth_code = _extract_oauth_code_from_page(_popup)
                        _save_cookies(_popup)
                        _popup_auth_result = (_popup_auth_code, True)

                    # Path B: popup on OAuth consent page → click Approve, wait for callback
                    elif ("oauth/authorize" in _tab_url or "claude.ai" in _tab_url) and (
                        "would like to connect" in _tab_body.lower()
                        or "your account will be used" in _tab_body.lower()
                        or "account to authenticate" in _tab_body.lower()
                        or "approve" in _tab_body.lower()
                    ):
                        _log("Browser: popup on consent page — clicking Approve in popup")
                        _approved = _click_approve(_popup)
                        if _approved:
                            _log("Browser: Approve clicked in popup — polling for callback URL")
                            # Log URL immediately after click to diagnose timing
                            try:
                                _log(f"Browser: popup URL 0.1s after Approve: {_popup.url[:120]}")
                            except Exception as _ue0:
                                _log(f"Browser: popup already closed 0.1s after Approve: {_ue0}")
                            # Poll popup URL every 0.5s for up to 25 seconds
                            _popup_ac = None
                            _popup_ok = False
                            for _poll_i in range(50):  # 50 × 0.5s = 25s
                                try:
                                    _poll_url = _popup.url
                                    if _poll_i % 4 == 0:  # log every 2s
                                        _log(f"Browser: popup poll #{_poll_i} URL: {_poll_url[:120]}")
                                    if "platform.claude.com" in _poll_url or _is_callback_url(_poll_url):
                                        _log(f"Browser: popup reached callback at poll #{_poll_i}: {_poll_url[:120]}")
                                        try:
                                            _popup.wait_for_load_state("networkidle", timeout=8000)
                                        except Exception:
                                            pass
                                        time.sleep(2)
                                        _popup_ac = _extract_oauth_code_from_page(_popup)
                                        _popup_ok = True
                                        break
                                except Exception as _poll_e:
                                    _log(f"Browser: popup closed/error at poll #{_poll_i}: {_poll_e}")
                                    break
                                time.sleep(0.5)
                            # If popup closed before we caught callback, check main page and all context pages.
                            # OAuth servers often navigate the opener (main page) to the callback URL
                            # when Authorize is clicked inside a popup.
                            if not _popup_ok:
                                try:
                                    _main_url = page.url
                                    _log(f"Browser: popup closed — checking main page URL: {_main_url[:120]}")
                                    if "platform.claude.com" in _main_url or _is_callback_url(_main_url):
                                        _log("Browser: main page is on callback URL — extracting code from main page")
                                        _popup_ac = _extract_oauth_code_from_page(page)
                                        _popup_ok = True
                                except Exception as _mpe:
                                    _log(f"Browser: error reading main page after popup close: {_mpe}")
                                if not _popup_ok:
                                    try:
                                        for _ctxp in page.context.pages:
                                            try:
                                                _ctxu = _ctxp.url
                                                if ("platform.claude.com" in _ctxu or _is_callback_url(_ctxu)) and _ctxp != page:
                                                    _log(f"Browser: found callback in context page: {_ctxu[:120]}")
                                                    _popup_ac = _extract_oauth_code_from_page(_ctxp)
                                                    _popup_ok = True
                                                    break
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                            if _popup_ok:
                                try:
                                    _save_cookies(_popup if not _popup.is_closed() else page)
                                except Exception:
                                    _save_cookies(page)
                            _popup_auth_result = (_popup_ac, _popup_ok)
                            _log(f"Browser: popup consent flow result: ok={_popup_ok}, code={_popup_ac[:20] if _popup_ac else None}")
                        else:
                            _log(f"Browser: could not find Approve button. Page: {_tab_body[:200]}")

                    # Path C: try to extract 6-digit code from popup body
                    else:
                        _m = _re2.search(r'\b(\d{6})\b', _tab_body)
                        if _m:
                            six_digit_code = _m.group(1)
                            _log(f"Browser: extracted 6-digit code from popup: {six_digit_code}")
                        else:
                            # Fallback: wait longer (JS may still be rendering)
                            time.sleep(3)
                            try:
                                _tab_body2 = _popup.inner_text("body") or ""
                            except Exception:
                                _tab_body2 = ""
                            if _tab_body2 != _tab_body:
                                _log(f"Browser: [magic-link-popup] body after extra wait: {_tab_body2[:400]}")
                            _m2 = _re2.search(r'\b(\d{6})\b', _tab_body2)
                            if _m2:
                                six_digit_code = _m2.group(1)
                                _log(f"Browser: extracted 6-digit code (after extra wait): {six_digit_code}")

                except Exception as _tab_e:
                    _log(f"Browser: error in popup for magic link: {_tab_e}")
                    import traceback as _tb
                    _log(f"Browser: magic-link-popup traceback: {_tb.format_exc()[:400]}")
                finally:
                    if _popup:
                        try:
                            _popup.close()
                        except Exception:
                            pass

                # If popup completed the full auth flow, return immediately
                if _popup_auth_result is not None:
                    _auth_code_from_popup, _ok_from_popup = _popup_auth_result
                    return _ok_from_popup, _auth_code_from_popup

                if six_digit_code:
                    break

            if not six_digit_code:
                _log("Browser: could not extract 6-digit verification code from any magic link.")
                return False, None

            # Enter the code into the input field on the current (original) page
            try:
                _verification_input.fill(six_digit_code)
            except Exception:
                # Re-query in case field was re-rendered
                for _vsel2 in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]',
                               'input[name="code"]', 'input[type="text"][maxlength="6"]']:
                    try:
                        _vi2 = page.query_selector(_vsel2)
                        if _vi2 and _vi2.is_visible():
                            _vi2.fill(six_digit_code)
                            break
                    except Exception:
                        pass
            _log(f"Browser: entered verification code {six_digit_code} into input field")
            time.sleep(0.5)

            # Submit the code
            _submit_btn = None
            for _sbtn_sel in [
                'button[type="submit"]',
                'button:has-text("Verify")',
                'button:has-text("Continue")',
                'button:has-text("Sign in")',
            ]:
                try:
                    _sb = page.query_selector(_sbtn_sel)
                    if _sb and _sb.is_visible():
                        _submit_btn = _sb
                        break
                except Exception:
                    pass
            if _submit_btn:
                _submit_btn.click()
                _log("Browser: clicked submit button for verification code")
            else:
                page.keyboard.press("Enter")
                _log("Browser: pressed Enter to submit verification code")

            # Wait for redirect after code submission (up to 15s)
            for _wi in range(15):
                time.sleep(1)
                if (_is_callback_url(page.url)
                        or "consent" in page.url
                        or "authorize" in page.url):
                    break
            _log(f"Browser: after code submission, URL = {page.url[:120]}")

            if _is_callback_url(page.url):
                auth_code = (
                    _extract_oauth_code_from_page(page)
                    if "platform.claude.com" in page.url else None
                )
                _save_cookies(page)
                return True, auth_code

            # May land on OAuth consent screen
            time.sleep(2)
            _log("Browser: looking for Approve button after verification code submission...")
            approved = _click_approve(page)
            if approved:
                _log("Browser: Approve clicked — waiting for callback redirect...")
                auth_code, ok = _wait_for_callback_and_extract(page)
                if ok:
                    _save_cookies(page)
                return ok, auth_code

            _log(f"Browser: no callback or approve after code submission. URL: {page.url[:120]}")
            try:
                _log(f"Browser: page body after code: {page.inner_text('body')[:600]}")
            except Exception:
                pass
            return False, None

        # ── Legacy flow: navigate magic link directly in this tab ────────────
        # n8n sends ALL unread Anthropic emails — the inbox has many old ones.
        # Old magic links are single-use and already consumed → "unable to verify".
        # The NEWEST email has the fresh link for this session.
        # We collected URLs in arrival order (oldest first), so iterate REVERSED.
        for _ml_idx, magic_url in enumerate(reversed(magic_urls)):
            _log(f"Browser: trying magic link {_ml_idx + 1}/{len(magic_urls)}: {magic_url[:100]}...")
            try:
                page.goto(magic_url, timeout=30000)
            except Exception as _nav_e:
                _log(f"Browser: magic link navigation warning (may be normal): {_nav_e}")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                time.sleep(3)
            _log(f"Browser: after magic link, URL = {page.url[:120]}")

            # Check if this magic link was already consumed (stale)
            try:
                _ml_body = page.inner_text("body")
            except Exception:
                _ml_body = ""
            if "unable to verify" in _ml_body.lower() or "problem persists" in _ml_body.lower():
                _log(f"Browser: magic link {_ml_idx + 1} is stale ('unable to verify') — trying next")
                continue

            _log(f"Browser: page title = {page.title()!r}")
            _log(f"Browser: page content sample: {page.content()[:400]}")

            # ── Step 4: Handle post-magic-link state ──────────────────────
            if _is_callback_url(page.url):
                _log("Browser: callback after magic link navigation ✓")
                auth_code = (
                    _extract_oauth_code_from_page(page)
                    if "platform.claude.com" in page.url else None
                )
                _save_cookies(page)
                return True, auth_code

            # Check if the magic link page itself shows a verification code
            # (new Anthropic flow applied on a direct navigation, not a code input)
            _ml_code_match = _re2.search(r'\b(\d{6})\b', _ml_body)
            if _ml_code_match and "verification" in _ml_body.lower():
                _log(f"Browser: magic link page shows verification code {_ml_code_match.group(1)} "
                     f"— cannot enter it (navigated away from original OAuth page). "
                     f"Try next magic link or manual intervention.")
                continue

            _log("Browser: looking for Approve button on consent screen...")
            # Wait for React to render the consent UI
            time.sleep(2)
            _log(f"Browser: consent screen body (first 600): {_ml_body[:600]}")
            approved = _click_approve(page)
            if approved:
                _log("Browser: Approve clicked — waiting for callback redirect...")
                auth_code, ok = _wait_for_callback_and_extract(page)
                if ok:
                    _save_cookies(page)
                return ok, auth_code

            _log(f"Browser: no Approve button on magic link {_ml_idx + 1}. "
                 f"URL: {page.url[:120]}")
            # Don't give up — try next magic link if available
            continue

        _log("Browser: all magic link(s) exhausted without success.")
        return False, None

    # ── Attempt 1: camoufox (patched Firefox — CF-bypass) ───────────────
    try:
        from camoufox.sync_api import Camoufox
        _log("Browser: using camoufox (patched Firefox — CF-bypass mode).")
        try:
            with Camoufox(headless=True, geoip=True) as browser:
                page = browser.new_page()
                # Force English regardless of the container's GeoIP (Railway NL
                # datacenter causes Anthropic to serve French without this header,
                # breaking the English-only error-phrase detection below).
                try:
                    page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
                except Exception:
                    pass
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
            # Belt-and-suspenders: locale="en-US" is set on the context above,
            # but the HTTP header ensures Anthropic's server returns English even
            # if the navigator locale hint is overridden by Railway's GeoIP.
            page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
            result = _page_flow(page)
            browser.close()
            return result
    except Exception as e:
        _log(f"Browser automation error (playwright): {e}")
        import traceback
        _log(f"Browser traceback: {traceback.format_exc()[:600]}")
        return False, None


def _is_callback_url(url: str) -> bool:
    """
    Return True if the URL is a real OAuth callback landing page.

    IMPORTANT: claude.ai/oauth/authorize contains redirect_uri=https://platform.claude.com/
    oauth/code/callback URL-encoded as a query parameter.  A simple substring check like
    "callback" in url or "/callback" in url matches the redirect_uri param and causes
    false positives — the browser stays on the consent screen and never clicks Approve.

    We must check the URL *path* specifically, not the full URL string.
    Real callbacks are:
      - http://localhost:<port>/callback  (local dev mode, unlikely in container)
      - https://platform.claude.com/oauth/code/callback?code=...  (container/headless mode)
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        # localhost callback (any port, any path)
        if parsed.hostname and "localhost" in parsed.hostname:
            return True
        # platform.claude.com — must actually BE on that domain (not just in a param)
        if parsed.hostname == "platform.claude.com" and "/oauth" in parsed.path:
            return True
        # Direct path check — only the path component, not the full URL string
        if parsed.path.endswith("/callback") or "/oauth/code/callback" in parsed.path:
            return True
    except Exception:
        # Fallback for malformed URLs — be conservative (don't false-positive)
        if url.startswith("http://localhost"):
            return True
        if url.startswith("https://platform.claude.com/oauth"):
            return True
    return False


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
        # Check if we're already on a success/callback page before looking for buttons.
        # IMPORTANT: use _is_callback_url() — do NOT use "callback" in page.url because
        # the oauth/authorize URL contains redirect_uri=...%2Fcallback as a query param,
        # causing a false positive match that skips the Approve button entirely.
        if _is_callback_url(page.url):
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
            # Try nested structures — note: Claude CLI uses 'claudeAiOauth' (lowercase 'o')
            # not 'claudeAiOAuth'. ALL known variants are listed here.
            for _nested_key in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session", "credentials"):
                _nested = creds.get(_nested_key, {})
                if isinstance(_nested, dict):
                    refresh_token = (
                        _nested.get("refreshToken")
                        or _nested.get("refresh_token")
                        or _nested.get("oauthRefreshToken")
                    )
                    if refresh_token:
                        _log(f"Direct refresh: found refresh_token in nested key '{_nested_key}'")
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

        _json_body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }).encode()
        _form_body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }).encode()

        # Attempt 1: JSON body with browser-like headers on the confirmed real OAuth host.
        # Logs showed 405 (Method Not Allowed) on form-encoded — the endpoint exists but
        # likely requires JSON + Origin header. Try this before the form-encoded fallback.
        # Removed endpoints that always fail:
        #   api.claude.ai      → DNS nonexistent
        #   claude.ai/api/     → 403 Cloudflare
        #   api.anthropic.com  → 404
        _json_req = urllib.request.Request(
            "https://claude.com/cai/oauth/token",
            data=_json_body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; claude-cli/1.0)",
                "Origin": "https://claude.ai",
                "Referer": "https://claude.ai/",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(_json_req, timeout=15) as _jresp:
                _jresult = json.loads(_jresp.read().decode())
                if _jresult.get("access_token"):
                    _log("Direct refresh SUCCESS via claude.com/cai/oauth/token (JSON) ✓")
                    # Reuse the success/update block below by injecting into result loop
                    result = _jresult
                    new_access = result.get("access_token")
                    new_refresh = result.get("refresh_token", refresh_token)
                    # (update creds and return — same logic as loop below)
                    for _top_key in ("accessToken", "access_token"):
                        if _top_key in creds:
                            creds[_top_key] = new_access
                    for _top_key in ("refreshToken", "refresh_token", "oauthRefreshToken"):
                        if _top_key in creds:
                            creds[_top_key] = new_refresh
                    for _nested_key in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session", "credentials"):
                        _nested = creds.get(_nested_key, {})
                        if isinstance(_nested, dict):
                            for _ak in ("accessToken", "access_token"):
                                if _ak in _nested:
                                    _nested[_ak] = new_access
                            for _rk in ("refreshToken", "refresh_token"):
                                if _rk in _nested:
                                    _nested[_rk] = new_refresh
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
                    _log(f"Direct refresh (JSON): claude.com returned 200 but no access_token — "
                         f"keys: {list(_jresult.keys())}")
        except urllib.error.HTTPError as _je:
            try:
                _jbody = _je.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                _jbody = "(unreadable)"
            _log(f"Direct refresh (JSON): claude.com returned HTTP {_je.code} — body: {_jbody}")
        except Exception as _je:
            _log(f"Direct refresh (JSON): claude.com error — {_je}")

        # Attempt 2: form-encoded fallback on claude.ai (same path, different host)
        endpoints = [
            "https://claude.ai/cai/oauth/token",
        ]

        for endpoint in endpoints:
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=_form_body,
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
                        for _nested_key in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session", "credentials"):
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
            for _nested_key in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session"):
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

            _log(f"Proactive refresh: token expires in {int(remaining_s // 60)}m — trying direct refresh...")
        else:
            _log("Proactive refresh: expiresAt not found in credentials — attempting refresh anyway.")

        # Don't run if recovery is already in progress
        if _recovery_running.is_set():
            _log("Proactive refresh: recovery already in progress — skipping.")
            return True

        _refresh_ok = _try_direct_refresh()
        if _refresh_ok:
            return True
        # Direct refresh failed (Cloudflare blocks datacenter IPs for the OAuth endpoint).
        # If the token is within 2 hours of expiry, escalate to full_recovery_chain()
        # so a fresh token is obtained before any request fails.
        if expires_at_ms is not None and remaining_s < 7200:
            _log(
                f"Proactive refresh: direct refresh failed, token expires in "
                f"{int(remaining_s // 60)}m — escalating to full_recovery_chain()"
            )
            try:
                return full_recovery_chain()
            except Exception as _pe:
                _log(f"Proactive refresh: recovery escalation error — {_pe}")
        return False

    except Exception as _e:
        _log(f"Proactive refresh error: {_e}")
        return False


def _write_vault_auth_note(layer: str, duration_s: float) -> None:
    """
    Write an auth recovery event note to the Obsidian vault.
    Called on every successful full_recovery_chain() — builds an audit trail
    of when auth recovered, which layer succeeded, and how long it took.
    Runs directly via subprocess (we are already on inspiring-cat which has
    Railway internal network access to obsidian-vault). Never raises.
    """
    try:
        import subprocess as _sp
        import datetime as _dt
        now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        date = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        note_content = (
            f"# CLI Auth Recovery — {now}\n\n"
            f"**Layer:** {layer}\n"
            f"**Duration:** {duration_s:.0f}s\n"
            f"**Result:** SUCCESS ✓\n\n"
            f"Claude CLI Pro re-authenticated automatically via {layer}.\n"
            f"Credentials written to /root/.claude/.credentials.json and backed up to volume.\n"
        )
        note_repr = repr(note_content)
        path_repr = repr(f"Engineering/Auth Recovery {date} {layer}.md")
        script = (
            "import asyncio\n"
            f"NOTE = {note_repr}\n"
            f"PATH = {path_repr}\n"
            "async def main():\n"
            "    from mcp.client.sse import sse_client\n"
            "    from mcp import ClientSession\n"
            "    async with sse_client(url='http://obsidian-vault.railway.internal:22360/sse') as (r, w):\n"
            "        async with ClientSession(r, w) as s:\n"
            "            await s.initialize()\n"
            "            await s.call_tool('write_file', {'path': PATH, 'content': NOTE})\n"
            "asyncio.run(main())\n"
        )
        _sp.run(["python3", "-c", script], timeout=15, capture_output=True)
        _log(f"Vault: auth recovery note written ({layer}, {duration_s:.0f}s)")
    except Exception as e:
        _log(f"Vault: auth recovery note skipped: {e}")


def _try_refresh_session_cookies() -> bool:
    """
    Lightweight browser ping to keep claude.ai session cookies fresh.

    Opens a headless browser with the saved cookies, navigates to claude.ai,
    and checks if the session is still active. If yes, the server issues
    refreshed Set-Cookie headers — we save the new cookies (extending their TTL).
    If the session has expired, returns False so the caller can do a full login.

    Does NOT go through the OAuth flow — just a plain claude.ai page load.
    Typical runtime: 5-10 seconds. Never raises.
    """
    import json as _json

    if not _COOKIES_FILE.exists():
        _log("Cookie keepalive: no cookie file found — skipping refresh")
        return False

    # Age guard: cookies older than 30 days are expired server-side — delete and skip
    try:
        _age_days = (time.time() - _COOKIES_FILE.stat().st_mtime) / 86400
        if _age_days > 30:
            _log(f"Cookie keepalive: cookie file is {_age_days:.0f} days old (>30) — "
                 "deleting stale file and skipping refresh")
            _COOKIES_FILE.unlink(missing_ok=True)
            return False
    except Exception:
        pass

    try:
        _saved = _json.loads(_COOKIES_FILE.read_text())
    except Exception as _e:
        _log(f"Cookie keepalive: cannot read cookie file — {_e}")
        return False

    def _check_session(page) -> bool:
        """Returns True if session is active and fresh cookies were saved."""
        try:
            page.context.add_cookies(_saved)
            page.goto("https://claude.ai", timeout=30000, wait_until="domcontentloaded")
            time.sleep(3)
            current_url = page.url
            # Active session: stays on claude.ai app, no redirect to /login or /auth
            if "/login" not in current_url and "/auth" not in current_url:
                fresh = page.context.cookies()
                claude_cookies = [c for c in fresh if "claude" in c.get("domain", "")]
                if claude_cookies:
                    _COOKIES_FILE.write_text(_json.dumps(fresh))
                    _log(f"Cookie keepalive: session active — saved {len(fresh)} fresh cookies ✓ "
                         f"({len(claude_cookies)} claude.ai cookies refreshed)")
                    return True
                _log("Cookie keepalive: session active but no claude cookies in response — skipping save")
                return True  # session ok, just no new cookies
            _log(f"Cookie keepalive: session expired — redirected to {current_url[:80]}")
            return False
        except Exception as _e:
            _log(f"Cookie keepalive: navigation error — {_e}")
            return False

    # Try camoufox first (CF-bypass), fall back to Playwright Chromium
    try:
        from camoufox.sync_api import Camoufox
        with Camoufox(headless=True, geoip=True) as browser:
            page = browser.new_page()
            return _check_session(page)
    except ImportError:
        pass
    except Exception as _e:
        _log(f"Cookie keepalive: camoufox error — {_e}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            result = _check_session(page)
            browser.close()
            return result
    except Exception as _e:
        _log(f"Cookie keepalive: Playwright error — {_e}")
        return False


def run_cookie_keepalive() -> bool:
    """
    Proactive session cookie refresh — run every 8 hours.

    Goal: ensure Layer 4 of the recovery chain (cookie reuse) always has
    valid cookies so that the next OAuth token expiry recovers in ~3 min
    (cookie shortcut) instead of ~10 min (full magic link flow).

    Strategy:
      1. Try lightweight _try_refresh_session_cookies() — navigates claude.ai
         and picks up freshened Set-Cookie headers. Takes ~5-10 seconds.
      2. If cookies have expired, run full auto_login_claude() to re-establish
         a fresh session. Takes ~3-10 minutes but resets the cookie clock.

    Never raises. Returns True if cookies are fresh after the call.
    """
    _log("Cookie keepalive: starting 8-hour proactive session refresh...")

    # Fast path — cookies still valid
    if _try_refresh_session_cookies():
        _log("Cookie keepalive: cookies refreshed successfully ✓ (Layer 4 is ready)")
        return True

    # Cookies expired — do a full login to re-establish them.
    # Guard: if recovery chain is already running (which spawns its own browser login),
    # skip to avoid two competing PTY sessions at boot.
    if _recovery_running.is_set():
        _log("Cookie keepalive: recovery chain already running a browser login — skipping to avoid duplicate PTY")
        return False
    _log("Cookie keepalive: cookies expired — running full auto_login_claude() to refresh...")
    try:
        ok = auto_login_claude()
        if ok:
            _log("Cookie keepalive: full login succeeded — fresh cookies saved ✓ (Layer 4 restored)")
            return True
        _log("Cookie keepalive: full login failed — cookies could NOT be refreshed")
        return False
    except Exception as _e:
        _log(f"Cookie keepalive: auto_login_claude() raised — {_e}")
        return False


def full_recovery_chain() -> bool:
    """
    Parallel recovery chain — all methods fire simultaneously at t=0.

    Methods run in parallel inside a ThreadPoolExecutor:
      • Direct OAuth refresh  — lightweight HTTP, ~2s (usually blocked by Cloudflare)
      • Browser auto-login    — camoufox/Playwright; tries saved session cookies
                                first (~3 min fast path) then magic link email
                                (~10 min slow path)

    Whichever method succeeds FIRST wins and the other is cancelled.

    Why parallel matters for the slow path:
      Serial:   try-cookies(3 min fail) → start-email → magic-link arrives → done  = ~13 min
      Parallel: start-email at t=0 simultaneously with cookie-check → magic-link
                arrives while (or just after) cookie-check runs                   = ~10 min

    NOTE: Env-var restore is intentionally OMITTED — this is only called after
    _try_restore_claude_auth() has already been exhausted by pro_router.

    CONCURRENCY GUARD: _recovery_lock ensures only ONE recovery chain runs at a
    time across ALL external callers (watchdog, pro_router, token_keeper). The
    parallelism here is within a single recovery run, not across callers.
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

    # ── Playwright backoff guard ─────────────────────────────────────────────
    # After a browser timeout (magic link email never arrived), back off 20 min
    # before retrying the full Playwright flow. Anthropic silently stops sending
    # magic link emails after 2-3 rapid requests — this prevents burning the quota.
    # Fast paths (direct refresh, cookie check) are unaffected; only the Playwright
    # escalation is blocked during the backoff window.
    global _last_playwright_timeout
    _elapsed_since_timeout = time.time() - _last_playwright_timeout
    if 0 < _elapsed_since_timeout < _PLAYWRIGHT_BACKOFF_S:
        _remaining_backoff = int((_PLAYWRIGHT_BACKOFF_S - _elapsed_since_timeout) // 60)
        _log(f"=== Playwright backoff: {_remaining_backoff}m remaining before next attempt "
             f"(prevents Anthropic email quota exhaustion) ===")
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
        # Poll until the flag CLEARS (recovery done). event.wait() returns immediately
        # when the event is already set — we need the opposite: wait for it to be cleared.
        _deadline = time.time() + 300
        while _recovery_running.is_set() and time.time() < _deadline:
            time.sleep(0.5)
        if _winner_succeeded(_t):
            _log("=== Recovery completed by another thread — fresh credentials detected ✓ ===")
            return True
        _log("=== Recovery completed by another thread — credentials NOT refreshed ===")
        return False

    if not _recovery_lock.acquire(blocking=False):
        _log("=== Recovery lock busy — waiting for existing recovery to finish ===")
        _t = time.time()
        # Same fix: poll until flag clears rather than using event.wait()
        _deadline = time.time() + 300
        while _recovery_running.is_set() and time.time() < _deadline:
            time.sleep(0.5)
        if _winner_succeeded(_t):
            return True
        return False

    # ── Cross-process file lock ──────────────────────────────────────────────
    # The threading.Lock above only prevents double-recovery within the same
    # process. If a shell task or external script calls full_recovery_chain()
    # while the cli_worker process is already recovering, the threading.Lock
    # is invisible to it. fcntl.flock() works across OS processes on the same
    # filesystem — the second caller gets LOCK_EX | LOCK_NB immediately, and
    # if it fails (EWOULDBLOCK), it backs off gracefully without consuming a
    # magic link email.
    _flock_fd = None
    try:
        import fcntl as _fcntl
        _FLOCK_PATH = Path("/workspace/.recovery_in_progress.lock")
        _flock_fd = open(_FLOCK_PATH, "w")
        _fcntl.flock(_flock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        # Lock acquired — we are the sole recovery process
    except (IOError, OSError) as _fle:
        _log(f"=== Cross-process recovery already running (flock: {_fle}) — backing off ===")
        try:
            if _flock_fd:
                _flock_fd.close()
        except Exception:
            pass
        _recovery_lock.release()
        return False
    except Exception as _fle2:
        # fcntl unavailable (Windows dev env, non-POSIX) — log and continue
        _log(f"=== Cross-process flock unavailable ({_fle2}) — proceeding without file lock ===")

    _recovery_started_at = time.time()
    _recovery_running.set()
    # Signal dashboard: recovery pipeline is now actively running
    try:
        from .agent_status_tracker import mark_recovering as _mark_recovering
        _mark_recovering("Claude CLI Pro")
    except Exception:
        pass
    try:
        import concurrent.futures as _cf
        _log("=== Starting parallel CLI recovery chain (all methods fire simultaneously) ===")

        # Both methods start at t=0. Whichever succeeds first wins.
        # Direct refresh is instant (~2s, usually Cloudflare-blocked but costs nothing to try).
        # Browser login tries saved session cookies first (~3 min fast path) then
        # submits the magic link email immediately (~10 min slow path) — critically,
        # the email is sent at t=0 rather than after the cookie check fails, so the
        # magic link is already waiting in the inbox when cookies are determined stale.
        # Shared race log — each attempt appends its result; the winner's
        # entry is written by the thread itself before returning ok=True.
        # After the race, any method that didn't complete is marked CANCELLED.
        _race_contestants: list[dict] = []

        def _direct_attempt() -> tuple[bool, str]:
            _t = time.time()
            ok = _try_direct_refresh()
            _race_contestants.append({
                "name": "Direct OAuth",
                "result": "WON" if ok else "FAILED",
                "duration_s": round(time.time() - _t, 1),
            })
            return ok, "Direct OAuth refresh"

        def _browser_attempt() -> tuple[bool, str]:
            global _playwright_attempts_log
            _t = time.time()
            # Track attempt timestamp; prune to last hour; alert if thrashing
            _playwright_attempts_log.append(_t)
            _playwright_attempts_log = [ts for ts in _playwright_attempts_log if _t - ts < 3600]
            if len(_playwright_attempts_log) > 3:
                _log(
                    f"[ALERT] {len(_playwright_attempts_log)} Playwright recovery attempts in the last hour "
                    f"(threshold: 3) — possible auth loop. Check n8n + Anthropic email delivery."
                )
                try:
                    from .pro_token_keeper import _send_vault_alert
                    _send_vault_alert(len(_playwright_attempts_log))
                except Exception:
                    pass
            result = auto_login_claude()
            ok = result[0] if isinstance(result, tuple) else bool(result)
            _race_contestants.append({
                "name": "Playwright Login",
                "result": "WON" if ok else "FAILED",
                "duration_s": round(time.time() - _t, 1),
            })
            return ok, "Playwright auto-login"

        # Use explicit pool management (NOT 'with') — ThreadPoolExecutor.__exit__
        # calls shutdown(wait=True) which would block for up to 10 min waiting
        # for the loser thread. We want the winner to return immediately.
        _pool = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="recovery")
        futures: list[_cf.Future] = []
        _stall_alerted = False
        try:
            f_direct  = _pool.submit(_direct_attempt)
            f_browser = _pool.submit(_browser_attempt)
            futures   = [f_direct, f_browser]

            for future in _cf.as_completed(futures, timeout=720):
                elapsed = time.time() - _recovery_started_at
                # Stall alert: fires once if neither thread has succeeded after 8 min.
                # Usually means the magic link email never arrived — n8n may be broken.
                if not _stall_alerted and elapsed > 480:
                    _stall_alerted = True
                    _log(f"[STALL] Recovery taking {elapsed:.0f}s — magic link may not have "
                         f"arrived. Check n8n workflow jun8CaMnNhux1iEY and Outlook inbox.")
                    try:
                        from .pro_token_keeper import _send_vault_alert
                        _send_vault_alert(0)
                    except Exception:
                        pass
                try:
                    ok, layer = future.result()
                    if ok:
                        _log(f"=== Recovery SUCCESS via {layer} ===")
                        _write_vault_auth_note(layer, time.time() - _recovery_started_at)
                        # Signal dashboard: worker is healthy again.
                        try:
                            from .agent_status_tracker import mark_done, record_recovery
                            mark_done("Claude CLI Pro")
                            # Mark any method that was cancelled (didn't complete before winner)
                            _competing = {"Direct OAuth", "Playwright Login"}
                            _completed = {c["name"] for c in _race_contestants}
                            for _cn in _competing - _completed:
                                _race_contestants.append({
                                    "name": _cn,
                                    "result": "CANCELLED",
                                    "duration_s": round(time.time() - _recovery_started_at, 1),
                                })
                            record_recovery("Claude CLI Pro", layer,
                                            time.time() - _recovery_started_at,
                                            contestants=list(_race_contestants))
                            _log("Recovery: dashboard status reset to idle ✓")
                        except Exception as _sd_err:
                            _log(f"Recovery: mark_done failed (non-fatal): {_sd_err}")
                        # Delete boot sentinel so the 30-min health check no longer
                        # re-marks sick on every cycle after recovery.
                        try:
                            import pathlib as _sp
                            _boot_sick = _sp.Path("/tmp/.claude_boot_sick")
                            if _boot_sick.exists():
                                _boot_sick.unlink(missing_ok=True)
                                _log("Recovery: boot sentinel removed ✓")
                        except Exception as _se:
                            _log(f"Recovery: sentinel cleanup failed (non-fatal): {_se}")
                        # Alert: notify that Claude CLI was refreshed / recovered
                        try:
                            from ..alerts.notifier import alert_claude_recovered
                            alert_claude_recovered(subscription="Pro")
                        except Exception:
                            pass
                        # Shut down without blocking — loser thread runs to
                        # completion in the background, doesn't delay our return.
                        _pool.shutdown(wait=False, cancel_futures=True)
                        return True
                except Exception as _attempt_err:
                    _log(f"Recovery attempt raised: {_attempt_err}")

        except _cf.TimeoutError:
            _log("=== Recovery timed out after 12 minutes — manual login required ===")
        finally:
            # Release pool resources on timeout / exception / normal exit
            _pool.shutdown(wait=False, cancel_futures=True)

        _log("=== ALL recovery methods FAILED — manual login required ===")
        # Record the timeout timestamp so the backoff guard blocks rapid retries.
        # Anthropic silently stops sending magic link emails after 2-3 requests —
        # the 20-min backoff gives their email system time to reset.
        _last_playwright_timeout = time.time()
        return False
    finally:
        _recovery_running.clear()
        _recovery_lock.release()
        # Release cross-process file lock
        if _flock_fd is not None:
            try:
                import fcntl as _fcntl
                _fcntl.flock(_flock_fd, _fcntl.LOCK_UN)
                _flock_fd.close()
            except Exception:
                pass
