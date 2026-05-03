"""
CLI Worker — FastAPI task queue service.

Runs on port 8002 inside the cli-worker Railway service.
Provides a durable queue for Claude/Gemini CLI subprocess calls,
backed by the shared Railway Postgres database.

Endpoints:
  GET  /health           → live CLI availability check
  POST /tasks            → submit a task, get back task_id
  GET  /tasks/{task_id}  → poll result (pending/running/done/failed)

Crash recovery:
  On boot, any task left in status=running for >3 min is requeued.
  The background loop picks it up and retries automatically.

Task types accepted:
  claude_pro, gemini_cli, claude_auth, claude_probe, gemini_probe, flutter_build
"""
import asyncio
import json
import os
import subprocess
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import migrations, task_runner

# ── Scheduler (CLI maintenance jobs live here, not in API service) ────────────
_scheduler = AsyncIOScheduler(timezone="UTC")

# ── Health check cache — avoids stacking parallel 20s claude -p probes ────────
_health_cache: dict = {}
_health_cache_ts: float = 0.0
_HEALTH_CACHE_TTL = 60  # seconds


def _bg_log(msg: str, source: str = "cli_worker") -> None:
    """Best-effort log — writes to stdout (Railway captures it)."""
    print(f"[{source}] {msg}", flush=True)


# ── Background task worker loop ───────────────────────────────────────────────

async def _worker_loop() -> None:
    """Poll Postgres for pending tasks every 2 seconds and execute them."""
    while True:
        try:
            task = task_runner.fetch_pending_task()
            if task:
                _bg_log(f"Running task {task['id']} type={task['type']}", "worker")
                # Run in thread pool so the event loop isn't blocked
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, task_runner.run_task_from_record, task)
        except Exception as e:
            _bg_log(f"Worker loop error: {e}", "worker")
        await asyncio.sleep(2)


# ── CLI maintenance scheduler jobs ───────────────────────────────────────────

def _pro_token_keeper_job() -> None:
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.pro_token_keeper import run_token_keeper
        result = run_token_keeper()
        status = "OK" if result.get("railway_ok") else "FAILED"
        _bg_log(
            f"Pro token keeper: {status} — ping={result.get('ping_ok')} "
            f"railway={result.get('railway_ok')} via={result.get('method')} "
            f"msg={result.get('message', '')[:120]}",
            "pro_token_keeper",
        )
    except Exception as e:
        _bg_log(f"Pro token keeper error: {e}", "pro_token_keeper")


def _gemini_token_keeper_job() -> None:
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.gemini_token_keeper import run_token_keeper as gemini_keep
        result = gemini_keep()
        status = "OK" if result.get("railway_ok") else "FAILED"
        _bg_log(
            f"Gemini token keeper: {status} — ping={result.get('ping_ok')} "
            f"railway={result.get('railway_ok')} via={result.get('method')} "
            f"msg={result.get('message', '')[:120]}",
            "gemini_token_keeper",
        )
    except Exception as e:
        _bg_log(f"Gemini token keeper error: {e}", "gemini_token_keeper")


def _pro_cli_watchdog_job() -> None:
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.pro_cli_watchdog import maybe_recover
        recovered = maybe_recover()
        if recovered:
            _bg_log("Pro CLI watchdog: recovery confirmed — Pro is primary again.", "pro_cli_watchdog")
    except Exception as e:
        _bg_log(f"Pro CLI watchdog error: {e}", "pro_cli_watchdog")


def _proactive_token_refresh_job() -> None:
    """
    Runs every 12 hours. Proactively renews the OAuth refresh_token even when
    the CLI is healthy — prevents silent expiry from inactivity.
    Escalates to full_recovery_chain() if the direct refresh fails.
    """
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.pro_token_keeper import run_proactive_refresh
        result = run_proactive_refresh()
        status = "OK" if (result.get("direct_refresh_ok") or result.get("recovery_ok")) else "FAILED"
        _bg_log(
            f"Proactive token refresh: {status} — "
            f"direct={result.get('direct_refresh_ok')} "
            f"recovery={result.get('recovery_ok')} "
            f"msg={result.get('message', '')[:120]}",
            "proactive_refresh",
        )
    except Exception as e:
        _bg_log(f"Proactive token refresh error: {e}", "proactive_refresh")


def _gemini_watchdog_job() -> None:
    """
    Runs every 90 seconds. Mirrors _pro_cli_watchdog_job() for Gemini CLI.
    If Gemini auth fails, immediately runs gemini_full_recovery() (direct
    OAuth refresh → env var restore) without waiting for the 4-hour keeper.
    """
    try:
        import sys
        sys.path.insert(0, "/app")
        # Fast check — reuse the already-present _gemini_auth_ok() in this module
        if _gemini_auth_ok():
            return  # all good, nothing to do
        _bg_log("Gemini watchdog: auth check failed — running recovery chain…", "gemini_watchdog")
        from app.learning.gemini_token_keeper import gemini_full_recovery
        ok = gemini_full_recovery()
        _bg_log(
            f"Gemini watchdog: recovery {'SUCCESS ✓' if ok else 'FAILED — manual gemini auth login required'}",
            "gemini_watchdog",
        )
    except Exception as e:
        _bg_log(f"Gemini watchdog error: {e}", "gemini_watchdog")


def _refresh_token_keepalive_job() -> None:
    """
    Runs weekly. Forces a full_recovery_chain() if the OAuth token has not been
    refreshed in the past 25 days — prevents the refresh_token itself from
    expiring due to inactivity (Anthropic expires stale refresh_tokens after ~30
    days; this job ensures there is always at least 5 days of headroom).
    """
    try:
        import sys
        import time as _t
        sys.path.insert(0, "/app")
        from app.learning.agent_status_tracker import get_worker_status
        from app.learning.cli_auto_login import full_recovery_chain
        cli_w = get_worker_status("Claude CLI Pro")
        last_recovery = cli_w.get("last_recovery_at") if cli_w else None
        if last_recovery is None or (_t.time() - last_recovery) > 25 * 86400:
            days_since = int((_t.time() - last_recovery) / 86400) if last_recovery else -1
            _bg_log(
                f"Refresh token keepalive: last refresh was "
                f"{'never' if days_since < 0 else f'{days_since}d ago'} — "
                f"triggering full_recovery_chain() to keep refresh_token alive",
                "keepalive",
            )
            ok = full_recovery_chain()
            _bg_log(
                f"Refresh token keepalive: {'SUCCESS ✓' if ok else 'FAILED — refresh_token may expire soon'}",
                "keepalive",
            )
        else:
            days_since = int((_t.time() - last_recovery) / 86400)
            _bg_log(
                f"Refresh token keepalive: last refresh was {days_since}d ago — still fresh, nothing to do",
                "keepalive",
            )
    except Exception as e:
        _bg_log(f"Refresh token keepalive error: {e}", "keepalive")


def _cookie_keepalive_job() -> None:
    """
    Runs every 12 hours. Headless browser touches claude.ai to refresh the
    session so Layer 4 of the recovery chain (cookie reuse) always has a
    valid session.

    Without this, cookies expire independently of the OAuth token — the next
    recovery falls through to the 10-minute Playwright magic link flow.
    With this, the next recovery takes ~3 minutes (cookie shortcut path).
    """
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.cli_auto_login import run_cookie_keepalive
        ok = run_cookie_keepalive()
        _bg_log(
            f"Cookie keepalive: {'cookies fresh — Layer 4 ready ✓' if ok else 'FAILED — Layer 4 may not be available'}",
            "cookie_keepalive",
        )
    except Exception as e:
        _bg_log(f"Cookie keepalive error: {e}", "cookie_keepalive")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot: create tables, requeue stale tasks
    try:
        migrations.ensure_table()
        migrations.ensure_credentials_table()
        requeued = migrations.requeue_stale_tasks()
        if requeued:
            _bg_log(f"Boot: requeued {requeued} stale tasks (crash recovery)", "boot")
        else:
            _bg_log("Boot: DB ready, no stale tasks", "boot")
    except Exception as e:
        _bg_log(f"Boot DB error: {e} — task queue unavailable", "boot")

    # Boot: restore credentials from PostgreSQL (Layer 2b) if volume backup
    # is absent or stale.  This handles the scenario where the Railway volume
    # was remounted / path changed but the DB row is still fresh.
    try:
        import sys as _sys_creds
        _sys_creds.path.insert(0, "/app")
        from app.learning.cli_auto_login import _restore_credentials_from_db
        _vol_backup = __import__("pathlib").Path("/workspace/.claude_credentials_backup.json")
        # Attempt DB restore if volume backup is absent OR if it's expired.
        # An expired backup on disk should not block a fresher DB record from restoring.
        _vol_expired = False
        if _vol_backup.exists():
            try:
                import json as _jv, time as _tv
                _vd = _jv.loads(_vol_backup.read_text())
                _vm = None
                for _vk in ("expiresAt", "expires_at"):
                    if _vk in _vd: _vm = _vd[_vk]; break
                if _vm is None:
                    for _vnk in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session"):
                        _vn = _vd.get(_vnk, {})
                        if isinstance(_vn, dict):
                            _vm = _vn.get("expiresAt") or _vn.get("expires_at")
                        if _vm: break
                if _vm and (_tv.time() * 1000) > float(_vm) + 300_000:
                    _vol_expired = True
            except Exception:
                pass

        if not _vol_backup.exists() or _vol_expired:
            _reason = "absent" if not _vol_backup.exists() else "EXPIRED"
            _bg_log(f"Boot: volume backup {_reason} — attempting DB restore (Layer 2b)", "boot")
            if _restore_credentials_from_db():
                _bg_log("Boot: credentials restored from PostgreSQL ✓", "boot")
            else:
                _bg_log("Boot: DB restore returned nothing — will rely on env var / watchdog", "boot")
        else:
            _bg_log("Boot: volume backup present and valid — DB restore not needed", "boot")
    except Exception as _e:
        _bg_log(f"Boot: DB credential restore skipped — {_e}", "boot")

    # Boot: validate Layer 1 backup integrity — check that the backup file
    # exists AND contains a non-expired token.  If the backup is stale (written
    # with an already-expired token) we schedule an immediate full recovery
    # rather than waiting up to 90s for the watchdog to discover the bad creds.
    try:
        import json as _jboot
        from pathlib import Path as _Pboot
        _bpath = _Pboot("/workspace/.claude_credentials_backup.json")
        if _bpath.exists():
            _bd = _jboot.loads(_bpath.read_text())
            _bms = None
            for _bk in ("expiresAt", "expires_at"):
                if _bk in _bd:
                    _bms = _bd[_bk]; break
            if _bms is None:
                for _bnk in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session"):
                    _bn = _bd.get(_bnk, {})
                    if isinstance(_bn, dict):
                        for _bk in ("expiresAt", "expires_at"):
                            if _bk in _bn:
                                _bms = _bn[_bk]; break
                    if _bms is not None:
                        break
            if _bms is not None:
                _remaining = int((_bms - _time.time() * 1000) / 1000)
                if _remaining > 0:
                    _bg_log(f"Boot: Layer 1 backup valid — expires in {_remaining // 3600}h {(_remaining % 3600) // 60}m", "boot")
                else:
                    _bg_log(f"Boot: Layer 1 backup EXPIRED ({abs(_remaining) // 60}m ago) — scheduling immediate recovery", "boot")
                    def _boot_recover():
                        try:
                            import sys as _s; _s.path.insert(0, "/app")
                            from app.learning.cli_auto_login import full_recovery_chain as _frc
                            _bg_log("Boot: immediate recovery chain started", "boot")
                            ok = _frc()
                            _bg_log(f"Boot: immediate recovery chain {'SUCCEEDED ✓' if ok else 'FAILED — watchdog will retry'}", "boot")
                        except Exception as _re:
                            _bg_log(f"Boot: immediate recovery error — {_re}", "boot")
                    asyncio.get_event_loop().run_in_executor(None, _boot_recover)
            else:
                _bg_log("Boot: Layer 1 backup present but no expiresAt found — watchdog will verify", "boot")
        else:
            _bg_log("Boot: Layer 1 backup absent — watchdog will recover", "boot")
    except Exception as _be:
        _bg_log(f"Boot: Layer 1 integrity check skipped — {_be}", "boot")

    # Boot: clear stale restore counter from the previous container run.
    # _RESTORE_COUNT_FLAG tracks consecutive failed credential restores; if it
    # survived a restart it would force an immediate Playwright escalation on
    # the very first auth error, skipping the cheaper layers.
    try:
        import sys as _sys_boot
        _sys_boot.path.insert(0, "/app")
        from app.learning.pro_router import _RESTORE_COUNT_FLAG
        _RESTORE_COUNT_FLAG.unlink(missing_ok=True)
        _bg_log("Boot: stale restore counter cleared", "boot")
    except Exception as _e:
        _bg_log(f"Boot: restore counter clear skipped — {_e}", "boot")

    # Start background worker loop
    asyncio.create_task(_worker_loop())

    # Start CLI maintenance scheduler
    _scheduler.add_job(_pro_token_keeper_job, "interval", minutes=15,
                       id="pro_token_keeper", replace_existing=True)
    _scheduler.add_job(_gemini_token_keeper_job, "interval", hours=4,
                       id="gemini_token_keeper", replace_existing=True)
    _scheduler.add_job(_pro_cli_watchdog_job, "interval", seconds=90,
                       id="pro_cli_watchdog", replace_existing=True)
    _scheduler.add_job(_proactive_token_refresh_job, "interval", hours=12,
                       id="proactive_token_refresh", replace_existing=True)
    _scheduler.add_job(_gemini_watchdog_job, "interval", seconds=90,
                       id="gemini_watchdog", replace_existing=True)
    # cookie_keepalive is staggered 6h from proactive_token_refresh so they never
    # fire simultaneously and launch competing headless browser instances.
    _scheduler.add_job(_cookie_keepalive_job, "interval", hours=12,
                       id="cookie_keepalive", replace_existing=True,
                       start_date=datetime.now(timezone.utc) + timedelta(hours=6))
    # Refresh token keepalive: weekly job that forces a full recovery if the
    # OAuth token hasn't been refreshed in 25 days — prevents refresh_token expiry.
    # Staggered 3h from boot so it doesn't race with the startup watchdog.
    _scheduler.add_job(_refresh_token_keepalive_job, "interval", days=7,
                       id="refresh_token_keepalive", replace_existing=True,
                       start_date=datetime.now(timezone.utc) + timedelta(hours=3))
    _scheduler.start()
    _bg_log("CLI maintenance scheduler started (pro_keeper 15min, gemini_keeper 4h, claude_watchdog 90s, gemini_watchdog 90s, proactive_refresh 12h, cookie_keepalive 12h+6h offset, refresh_keepalive 7d)", "boot")

    # Boot-time: run watchdog immediately so CLI health is confirmed before the
    # first real request (normally would wait 90s). Run cookie keepalive too so
    # Layer 4 (cookie shortcut) is primed from the start, not after 6h.
    # Both run in the thread pool — non-blocking, fire-and-forget.
    _loop = asyncio.get_event_loop()
    _loop.run_in_executor(None, _pro_cli_watchdog_job)
    _loop.run_in_executor(None, _cookie_keepalive_job)
    _loop.run_in_executor(None, _pro_token_keeper_job)
    _loop.run_in_executor(None, _gemini_token_keeper_job)
    _bg_log("Boot: watchdog + cookie keepalive + pro/gemini token keepers fired immediately", "boot")

    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="CLI Worker", lifespan=lifespan)


# ── Schemas ───────────────────────────────────────────────────────────────────

class TaskSubmit(BaseModel):
    type: str          # claude_pro | gemini_cli | claude_auth | claude_probe | gemini_probe | flutter_build
    payload: dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db_conn():
    url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def _probe(cmd: list[str], timeout: int = 10) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           env={**os.environ, "HOME": "/root"})
        return r.returncode == 0
    except Exception:
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _gemini_auth_ok() -> bool:
    """Check Gemini CLI binary AND credentials — version alone is not enough."""
    if not _probe(["gemini", "--version"]):
        return False
    # Verify credentials exist — check all known file locations
    import json as _json
    _gemini_dir = "/root/.gemini"
    _cred_candidates = [
        f"{_gemini_dir}/credentials.json",
        f"{_gemini_dir}/oauth_creds.json",
        f"{_gemini_dir}/auth.json",
    ]
    for creds_path in _cred_candidates:
        try:
            with open(creds_path, "r") as f:
                creds = _json.load(f)
            # Any non-empty JSON with auth-related keys counts
            if creds.get("client_id") or creds.get("refresh_token") or creds.get("api_key"):
                return True
        except Exception:
            continue
    # Also check settings.json for API key auth
    try:
        with open(f"{_gemini_dir}/settings.json", "r") as f:
            settings = _json.load(f)
        if settings.get("apiKey"):
            return True
    except Exception:
        pass
    # Check GEMINI_API_KEY env var
    if os.environ.get("GEMINI_API_KEY"):
        return True
    return False


def _probe_claude_prompt(timeout: int = 20) -> bool:
    """
    Test whether the Claude prompt is actually responsive — not just whether
    the binary exists. Sends a real `-p` prompt and checks for non-empty output.
    Falls back to version check if the prompt probe hangs or errors.
    """
    try:
        r = subprocess.run(
            ["claude", "-p", "ok"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env={**os.environ, "HOME": "/root"},
        )
        # returncode 0 means auth is valid — stdout may be empty for short prompts
        if r.returncode == 0:
            return True
        # Non-zero exit with auth-related error → definitely down
        combined = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace").lower()
        if any(p in combined for p in ("authentication", "login", "unauthorized", "token")):
            return False
        # returncode != 0 but no clear auth error → treat as down
        return False
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        # Binary missing or other OS error — fall back to version probe
        return _probe(["claude", "--version"])


def _read_token_expiry() -> dict:
    """Read token expiry info from credentials file. Never raises. Returns dict."""
    try:
        import json as _j
        _creds_path = "/root/.claude/.credentials.json"
        creds = _j.loads(open(_creds_path).read())
        expires_at_ms = None
        for _k in ("expiresAt", "expires_at"):
            if _k in creds:
                expires_at_ms = creds[_k]
                break
        if expires_at_ms is None:
            for _nk in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session"):
                _n = creds.get(_nk, {})
                if isinstance(_n, dict):
                    for _k in ("expiresAt", "expires_at"):
                        if _k in _n:
                            expires_at_ms = _n[_k]
                            break
                if expires_at_ms is not None:
                    break
        if expires_at_ms is not None:
            remaining_s = int((expires_at_ms - _time.time() * 1000) / 1000)
            return {"expires_in_s": remaining_s, "expires_at_ms": int(expires_at_ms)}
    except Exception:
        pass
    return {"expires_in_s": None, "expires_at_ms": None}


_SUPER_AGENT_URL = os.environ.get("SUPER_AGENT_URL", "https://super-agent-production.up.railway.app")

@app.get("/observability")
@app.get("/intelligence")
@app.get("/monitoring")
@app.get("/agents")
@app.get("/spend")
def redirect_to_super_agent_dashboard(request: Request):
    """Redirect inspiring-cat dashboard links to the super-agent dashboards."""
    path = request.url.path
    return RedirectResponse(url=f"{_SUPER_AGENT_URL}{path}", status_code=302)


@app.get("/health")
def health():
    """
    Live health check — verifies Claude and Gemini CLIs are responding.
    Uses an actual prompt probe for Claude (not just --version) so a hung
    or unresponsive prompt is correctly detected as unhealthy.
    Called by pro_cli_watchdog and pro_router.verify_pro_auth().

    Result is cached for 60s so parallel callers (watchdog, router, monitors)
    don't stack simultaneous 20s claude -p probes.
    """
    global _health_cache, _health_cache_ts
    if _health_cache and (_time.time() - _health_cache_ts) < _HEALTH_CACHE_TTL:
        return _health_cache

    claude_ok  = _probe_claude_prompt()
    gemini_ok  = _gemini_auth_ok()
    db_ok = False
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass

    expiry = _read_token_expiry()
    result = {
        "status": "ok" if (claude_ok and db_ok) else "degraded",
        "claude_available": claude_ok,
        "gemini_available": gemini_ok,
        "db_connected": db_ok,
        "claude_token_expires_in_s": expiry["expires_in_s"],
    }
    _health_cache = result
    _health_cache_ts = _time.time()
    return result


@app.get("/health/detailed")
def health_detailed():
    """
    Detailed layer-by-layer status. Read-only — no side effects.
    Returns token TTL, backup integrity, cookie freshness, and recovery history
    so external monitors and the dashboard can display full system state.
    """
    import json as _j
    from pathlib import Path as _P

    # Token expiry
    expiry = _read_token_expiry()

    # Layer 1: backup integrity
    _backup_path = _P("/workspace/.claude_credentials_backup.json")
    layer1_exists = _backup_path.exists()
    layer1_expires_in_s = None
    layer1_valid = False
    if layer1_exists:
        try:
            bd = _j.loads(_backup_path.read_text())
            _bms = None
            for _k in ("expiresAt", "expires_at"):
                if _k in bd:
                    _bms = bd[_k]; break
            if _bms is None:
                for _nk in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session"):
                    _n = bd.get(_nk, {})
                    if isinstance(_n, dict):
                        for _k in ("expiresAt", "expires_at"):
                            if _k in _n:
                                _bms = _n[_k]; break
                    if _bms is not None:
                        break
            if _bms is not None:
                layer1_expires_in_s = int((_bms - _time.time() * 1000) / 1000)
                layer1_valid = layer1_expires_in_s > 0
        except Exception:
            pass

    # Layer 4: browser cookies
    _cookies_path = _P("/workspace/.claude_browser_cookies.json")
    layer4_cookies_exist = _cookies_path.exists()
    layer4_cookies_age_s = None
    if layer4_cookies_exist:
        try:
            layer4_cookies_age_s = int(_time.time() - _cookies_path.stat().st_mtime)
        except Exception:
            pass

    # Recovery history from agent_status_tracker
    last_recovery_at = None
    last_recovery_layer = None
    recovery_count_today = 0
    playwright_attempts_last_1h = 0
    try:
        import sys as _sys
        _sys.path.insert(0, "/app")
        from app.learning.agent_status_tracker import get_worker_status
        w = get_worker_status("Claude CLI Pro")
        if w.get("last_recovery_at"):
            last_recovery_at = datetime.fromtimestamp(
                w["last_recovery_at"], tz=timezone.utc
            ).isoformat()
        last_recovery_layer = w.get("last_recovery_layer")
        recovery_count_today = w.get("recovery_count_today", 0)
    except Exception:
        pass
    try:
        from app.learning.cli_auto_login import _playwright_attempts_log
        _now = _time.time()
        playwright_attempts_last_1h = sum(1 for ts in _playwright_attempts_log if _now - ts < 3600)
    except Exception:
        pass

    # Layer 4 infra: n8n reachability + verification workflow status (cached 60s)
    n8n_reachable = None
    n8n_verification_workflow_active = None
    n8n_verification_workflow_name = None
    _N8N_CACHE_TTL = 60
    _n8n_cache_key = "health_detailed_n8n"
    _n8n_cached = getattr(app.state, _n8n_cache_key, None) if hasattr(app, "state") else None
    _n8n_cache_ts = getattr(app.state, _n8n_cache_key + "_ts", 0) if hasattr(app, "state") else 0
    if _n8n_cached is not None and (_time.time() - _n8n_cache_ts) < _N8N_CACHE_TTL:
        n8n_reachable = _n8n_cached.get("reachable")
        n8n_verification_workflow_active = _n8n_cached.get("active")
        n8n_verification_workflow_name = _n8n_cached.get("name")
    else:
        try:
            import urllib.request as _ur, json as _jn
            _n8n_url = os.environ.get("N8N_BASE_URL", "").rstrip("/")
            _n8n_key = os.environ.get("N8N_API_KEY", "")
            _WF_ID = "jun8CaMnNhux1iEY"
            if _n8n_url:
                _req = _ur.Request(f"{_n8n_url}/api/v1/workflows/{_WF_ID}",
                                   headers={"X-N8N-API-KEY": _n8n_key})
                with _ur.urlopen(_req, timeout=4) as _r:
                    _wf = _jn.loads(_r.read())
                n8n_reachable = True
                n8n_verification_workflow_active = _wf.get("active", False)
                n8n_verification_workflow_name = _wf.get("name", _WF_ID)
            else:
                n8n_reachable = False
        except Exception:
            n8n_reachable = False
        if hasattr(app, "state"):
            setattr(app.state, _n8n_cache_key, {
                "reachable": n8n_reachable,
                "active": n8n_verification_workflow_active,
                "name": n8n_verification_workflow_name,
            })
            setattr(app.state, _n8n_cache_key + "_ts", _time.time())

    # Playwright + camoufox availability (cached 300s — only changes on redeploy)
    playwright_ok = None
    camoufox_ok = None
    _PW_CACHE_TTL = 300
    _pw_cache_ts = getattr(app.state, "pw_check_ts", 0) if hasattr(app, "state") else 0
    if (_time.time() - _pw_cache_ts) < _PW_CACHE_TTL:
        playwright_ok = getattr(app.state, "pw_ok", None)
        camoufox_ok = getattr(app.state, "camoufox_ok", None)
    else:
        try:
            from playwright.sync_api import sync_playwright as _spw
            with _spw() as _pw:
                _b = _pw.chromium.launch(headless=True); _b.close()
            playwright_ok = True
        except Exception:
            playwright_ok = False
        try:
            import camoufox as _cfx  # noqa: F401
            camoufox_ok = True
        except Exception:
            camoufox_ok = False
        if hasattr(app, "state"):
            setattr(app.state, "pw_ok", playwright_ok)
            setattr(app.state, "camoufox_ok", camoufox_ok)
            setattr(app.state, "pw_check_ts", _time.time())

    return {
        "token_expires_in_s": expiry["expires_in_s"],
        "token_expires_at_ms": expiry["expires_at_ms"],
        "layer1_backup_exists": layer1_exists,
        "layer1_backup_valid": layer1_valid,
        "layer1_backup_expires_in_s": layer1_expires_in_s,
        "layer4_cookies_exist": layer4_cookies_exist,
        "layer4_cookies_age_s": layer4_cookies_age_s,
        "last_recovery_at": last_recovery_at,
        "last_recovery_layer": last_recovery_layer,
        "recovery_count_today": recovery_count_today,
        "playwright_attempts_last_1h": playwright_attempts_last_1h,
        "n8n_reachable": n8n_reachable,
        "n8n_verification_workflow_active": n8n_verification_workflow_active,
        "n8n_verification_workflow_name": n8n_verification_workflow_name,
        "playwright_ok": playwright_ok,
        "camoufox_ok": camoufox_ok,
    }


@app.post("/webhook/verification-code")
async def receive_verification_code(body: dict, req: Request):
    """
    Receive magic link URL (or 6-digit code) from n8n for automated Claude CLI re-login.
    Claude.ai sends a MAGIC LINK email — n8n extracts the URL and POSTs it here.

    The Playwright browser automation runs inside THIS container (VS-Code-inspiring-cat).
    The magic link URL must be placed in THIS process's _verification_code_queue so the
    waiting browser thread can navigate to it.

    Security: if WEBHOOK_SECRET env var is set, the caller must supply the matching
    X-Webhook-Secret header. Requests with a wrong/missing secret are rejected 403.
    When WEBHOOK_SECRET is not set the check is skipped (backward compatible).
    """
    _expected_secret = os.environ.get("WEBHOOK_SECRET", "")
    if _expected_secret:
        _incoming_secret = req.headers.get("X-Webhook-Secret", "")
        if _incoming_secret != _expected_secret:
            _bg_log("Verification webhook: rejected — invalid or missing X-Webhook-Secret", "webhook")
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # Accept magic link URL (new) or legacy 6-digit code
    auth_payload = (
        str(body.get("url", "")).strip()
        or str(body.get("code", "")).strip()
    )
    if not auth_payload:
        return {"ok": False, "error": "No 'url' or 'code' field in payload"}

    # SSRF guard: reject URLs that don't point to Claude's own domains.
    if auth_payload.startswith("http://") or auth_payload.startswith("https://"):
        try:
            import sys as _sys
            _sys.path.insert(0, "/app")
            from app.security.ssrf import assert_safe_url as _assert_safe_url
            _assert_safe_url(
                auth_payload,
                allowed_domains=["claude.ai", "claude.com", "anthropic.com", "platform.claude.com"],
                resolve_dns=False,
            )
        except ValueError as _ssrf_err:
            _bg_log(f"Verification webhook: SSRF blocked — {_ssrf_err}", "webhook")
            return {"ok": False, "error": f"Rejected unsafe URL: {_ssrf_err}"}

    preview = auth_payload[:60] + "..." if len(auth_payload) > 60 else auth_payload
    _bg_log(f"Verification webhook: delivering magic link to local browser queue: {preview}", "webhook")

    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.cli_auto_login import receive_verification_code as _recv
        _recv(auth_payload)
        return {"ok": True, "message": "Magic link delivered to browser queue"}
    except Exception as e:
        _bg_log(f"Failed to deliver magic link to queue: {e}", "webhook")
        return {"ok": False, "error": str(e)}


@app.post("/webhook/github-oauth-result")
async def webhook_github_oauth_result(req: Request):
    """
    Receive the result of a GitHub Actions OAuth relay attempt.

    GitHub-hosted runners (Azure IPs) are not blocked by Anthropic's Cloudflare
    WAF, so they can POST to claude.com/cai/oauth/token on our behalf. This
    endpoint receives the result and delivers it to the waiting _try_direct_refresh()
    call via an in-memory queue keyed by the nonce.

    Security: caller must pass the OAUTH_RELAY_SECRET (set as a GitHub Actions
    secret and also as an env var in inspiring-cat). Requests with a wrong/missing
    secret are rejected with 403.

    Payload: {"nonce": "...", "http_code": 200, "body": {...}, "secret": "..."}
    """
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    expected_secret = os.environ.get("OAUTH_RELAY_SECRET", "")
    if expected_secret:
        if payload.get("secret", "") != expected_secret:
            _bg_log("github-oauth-result: rejected — wrong secret", "webhook")
            raise HTTPException(status_code=403, detail="Invalid secret")

    nonce = str(payload.get("nonce", "")).strip()
    if not nonce:
        raise HTTPException(status_code=400, detail="Missing nonce")

    _bg_log(f"github-oauth-result: received result for nonce={nonce[:12]}... "
            f"http_code={payload.get('http_code')}", "webhook")

    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.cli_auto_login import deliver_github_oauth_result
        deliver_github_oauth_result(nonce, payload)
        return {"ok": True}
    except Exception as e:
        _bg_log(f"github-oauth-result: delivery error — {e}", "webhook")
        return {"ok": False, "error": str(e)}


@app.post("/webhook/proactive-renewal")
async def webhook_proactive_renewal(req: Request):
    """
    Receive a raw OAuth response from the GitHub Actions proactive renewal cron
    and apply it to the local credentials file — bypassing n8n / Playwright / email.

    GitHub Actions runners use Azure IPs that are NOT blocked by Anthropic's
    Cloudflare WAF, so they successfully POST to claude.com/cai/oauth/token.
    This endpoint receives the result and patches the credentials file, then calls
    _push_token_to_railway() to propagate the fresh token everywhere.

    This is the n8n-independent backup path. When n8n is down, this keeps the
    token alive without any email magic-link flow.

    Security: INSPIRING_CAT_WEBHOOK_SECRET in both inspiring-cat Railway Variables
    and the GitHub Actions secret of the same name.

    Payload: {
        "secret": "<INSPIRING_CAT_WEBHOOK_SECRET>",
        "access_token": "...",
        "refresh_token": "...",
        "expires_in": 3600          # seconds
    }
    """
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    expected = os.environ.get("INSPIRING_CAT_WEBHOOK_SECRET", "") or os.environ.get("OAUTH_RELAY_SECRET", "")
    if not expected or payload.get("secret", "") != expected:
        _bg_log("proactive-renewal: rejected — wrong secret", "webhook")
        raise HTTPException(status_code=403, detail="Invalid secret")

    access_token  = str(payload.get("access_token", "")).strip()
    refresh_token = str(payload.get("refresh_token", "")).strip()
    expires_in    = int(payload.get("expires_in", 3600))

    if not access_token:
        raise HTTPException(status_code=400, detail="Missing access_token")

    try:
        import sys, json as _json, time as _time
        sys.path.insert(0, "/app")
        from pathlib import Path as _Path

        _CREDS_FILE = _Path("/root/.claude/.credentials.json")
        _CREDS_FILE2 = _Path("/root/.claude/credentials.json")

        # Load existing credentials to preserve structure; create minimal skeleton if absent
        creds = {}
        for _cf in (_CREDS_FILE, _CREDS_FILE2):
            try:
                if _cf.exists():
                    creds = _json.loads(_cf.read_text())
                    break
            except Exception:
                pass

        if not creds:
            creds = {"claudeAiOauth": {}}

        expires_at_ms = int((_time.time() + expires_in) * 1000)

        # Patch all known token field variants so any CLI version picks it up
        for _top in ("accessToken", "access_token"):
            if _top in creds:
                creds[_top] = access_token
        for _top in ("refreshToken", "refresh_token", "oauthRefreshToken"):
            if _top in creds:
                creds[_top] = refresh_token

        for _nested_key in ("claudeAiOauth", "claudeAiOAuth", "oauth", "session", "credentials"):
            _nested = creds.get(_nested_key, {})
            if isinstance(_nested, dict) and _nested:
                for _ak in ("accessToken", "access_token"):
                    if _ak in _nested:
                        _nested[_ak] = access_token
                for _rk in ("refreshToken", "refresh_token", "oauthRefreshToken"):
                    if _rk in _nested:
                        _nested[_rk] = refresh_token
                for _ek in ("expiresAt", "expires_at"):
                    if _ek in _nested:
                        _nested[_ek] = expires_at_ms
                creds[_nested_key] = _nested

        # If credentials were minimal/empty, build a full structure the CLI recognises
        oauth = creds.setdefault("claudeAiOauth", {})
        oauth.setdefault("accessToken", access_token)
        oauth["accessToken"] = access_token
        oauth.setdefault("refreshToken", refresh_token)
        oauth["refreshToken"] = refresh_token
        oauth["expiresAt"] = expires_at_ms

        creds_json = _json.dumps(creds, indent=2)
        _CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CREDS_FILE.write_text(creds_json)
        _CREDS_FILE.chmod(0o600)
        try:
            _CREDS_FILE2.write_text(creds_json)
            _CREDS_FILE2.chmod(0o600)
        except Exception:
            pass

        # Push the fresh token everywhere (Railway var, super-agent, volume backup)
        from app.learning.cli_auto_login import _push_token_to_railway
        _push_token_to_railway()

        # Clear CLI_DOWN so inspiring-cat resumes immediately
        try:
            from app.learning.pro_router import clear_cli_down_flag, reset_pro_flag
            reset_pro_flag()
        except Exception:
            pass
        try:
            from app.learning.agent_status_tracker import mark_done
            mark_done("Claude CLI Pro")
        except Exception:
            pass

        _bg_log(
            f"proactive-renewal: credentials patched + pushed "
            f"(expires_in={expires_in}s, refresh_rotated={bool(refresh_token)})",
            "webhook"
        )
        return {"ok": True, "expires_in": expires_in, "pushed": True}

    except Exception as e:
        _bg_log(f"proactive-renewal: error — {e}", "webhook")
        return {"ok": False, "error": str(e)}


@app.post("/webhook/trigger-playwright-refresh")
async def webhook_trigger_playwright_refresh(req: Request):
    """
    Trigger inspiring-cat's full_recovery_chain() asynchronously.

    Called by GitHub Actions proactive_token_renewal.yml when the token TTL
    is low and direct OAuth is unavailable. The recovery runs in a background
    thread so this endpoint returns immediately (202) without waiting.

    Payload: {"secret": "<INSPIRING_CAT_WEBHOOK_SECRET>"}
    """
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    expected = os.environ.get("INSPIRING_CAT_WEBHOOK_SECRET", "") or os.environ.get("OAUTH_RELAY_SECRET", "")
    if not expected or payload.get("secret", "") != expected:
        _bg_log("trigger-playwright-refresh: rejected — wrong secret", "webhook")
        raise HTTPException(status_code=403, detail="Invalid secret")

    import threading

    def _run_recovery():
        try:
            import sys
            sys.path.insert(0, "/app")
            from app.learning.cli_auto_login import full_recovery_chain
            _bg_log("trigger-playwright-refresh: starting full_recovery_chain() in background", "webhook")
            ok = full_recovery_chain()
            _bg_log(
                f"trigger-playwright-refresh: full_recovery_chain() {'SUCCESS ✓' if ok else 'FAILED'}",
                "webhook",
            )
        except Exception as e:
            _bg_log(f"trigger-playwright-refresh: recovery error — {e}", "webhook")

    t = threading.Thread(target=_run_recovery, daemon=True)
    t.start()
    _bg_log("trigger-playwright-refresh: background recovery thread started", "webhook")
    return {"ok": True, "message": "Playwright recovery started in background"}


@app.post("/webhook/manual-auth-code")
def webhook_manual_auth_code(request: dict):
    """
    Accept an OAuth auth code from the user and deliver it to the waiting
    auto_login_claude() PTY — bypasses code-server terminal paste issues.

    Usage: POST {"code": "y4kP6IMcGrVt0wetai..."}
    See GET /auth/login-status for the active OAuth URL to open in your browser.
    """
    code = str(request.get("code", "")).strip()
    if not code:
        return {"ok": False, "error": "'code' field required"}
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.cli_auto_login import send_manual_auth_code
        result = send_manual_auth_code(code)
        return result
    except Exception as e:
        _bg_log(f"Manual auth code error: {e}", "webhook")
        return {"ok": False, "error": str(e)}


@app.get("/auth/login-status")
def auth_login_status():
    """Show the OAuth URL currently waiting for an auth code (if any)."""
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.cli_auto_login import get_active_oauth_url, _active_pty_master_fd
        url = get_active_oauth_url()
        return {
            "pty_active": _active_pty_master_fd is not None,
            "oauth_url": url or None,
            "instructions": (
                "1. Open oauth_url in your browser\n"
                "2. Enter email → click magic link → copy the auth code\n"
                "3. POST {\"code\": \"...\"} to /webhook/manual-auth-code"
            ) if url else "No active login session",
        }
    except Exception as e:
        return {"pty_active": False, "oauth_url": None, "error": str(e)}


@app.post("/tasks", status_code=201)
def submit_task(req: TaskSubmit):
    """
    Submit a CLI task. Returns task_id immediately.
    The background worker picks it up within 2 seconds.
    """
    valid_types = {"claude_pro", "gemini_cli", "claude_auth",
                   "claude_probe", "gemini_probe", "flutter_build", "shell"}
    if req.type not in valid_types:
        raise HTTPException(400, f"Unknown task type '{req.type}'. Valid: {valid_types}")
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cli_tasks (type, payload) VALUES (%s, %s) RETURNING id",
                    (req.type, json.dumps(req.payload))
                )
                task_id = str(cur.fetchone()[0])
            conn.commit()
        return {"task_id": task_id, "status": "pending"}
    except Exception as e:
        raise HTTPException(500, f"Failed to submit task: {e}")


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    """
    Poll task result. Keep polling until status=done or status=failed.
    Typical latency: 2-5s for probe tasks, 5-120s for Claude/Gemini calls.
    """
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, type, status, result, error, created_at, started_at, finished_at "
                    "FROM cli_tasks WHERE id=%s",
                    (task_id,)
                )
                row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Task {task_id} not found")
        cols = ["id", "type", "status", "result", "error",
                "created_at", "started_at", "finished_at"]
        task = {k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in zip(cols, row)}
        return task
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


@app.get("/tasks")
def list_recent_tasks(limit: int = 20):
    """List the most recent tasks (for debugging)."""
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, type, status, created_at, started_at, finished_at "
                    "FROM cli_tasks ORDER BY created_at DESC LIMIT %s",
                    (min(limit, 100),)
                )
                rows = cur.fetchall()
        cols = ["id", "type", "status", "created_at", "started_at", "finished_at"]
        tasks = [
            {k: (v.isoformat() if isinstance(v, datetime) else v)
             for k, v in zip(cols, row)}
            for row in rows
        ]
        return {"tasks": tasks, "count": len(tasks)}
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


# ── Unified Memory API ────────────────────────────────────────────────────────
# These endpoints bridge Claude Code (local) ↔ super-agent (Railway) ↔
# inspiring-cat (Railway) so all agents share one growing knowledge base.

class MemoryItem(BaseModel):
    content: str
    memory_type: str = "fact"       # fact | decision | preference | goal | problem
    importance: int = 3             # 1 (low) to 5 (critical)
    source: str = "claude_code"     # who is writing this
    session_id: str = "shared"


class MemoryIngestRequest(BaseModel):
    memories: list[MemoryItem]


@app.post("/memory/ingest")
def memory_ingest(req: MemoryIngestRequest, request: Request):
    """
    Accept memories from external agents (Claude Code, inspiring-cat, etc.)
    and store them in the shared PostgreSQL memory store.

    This is the write path for the unified cross-model memory system:
    - Claude Code pushes its local markdown memory files here
    - inspiring-cat CLI Pro pushes task insights here
    - Any agent can write facts/decisions/preferences

    Protected by MEMORY_INGEST_SECRET env var if set.
    """
    _secret = os.environ.get("MEMORY_INGEST_SECRET", "")
    if _secret:
        provided = request.headers.get("X-Memory-Secret", "")
        if provided != _secret:
            raise HTTPException(status_code=403, detail="Invalid secret")

    saved = 0
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.memory.vector_memory import ingest_external_memory
        for item in req.memories:
            ok = ingest_external_memory(
                content=item.content,
                memory_type=item.memory_type,
                importance=item.importance,
                source=item.source,
                session_id=item.session_id,
            )
            if ok:
                saved += 1
        _bg_log(f"memory/ingest: stored {saved}/{len(req.memories)} memories "
                f"from source={req.memories[0].source if req.memories else '?'}",
                "memory")
        return {"ok": True, "saved": saved, "total": len(req.memories)}
    except Exception as e:
        _bg_log(f"memory/ingest error: {e}", "memory")
        raise HTTPException(500, f"Memory ingest failed: {e}")


@app.get("/memory/export")
def memory_export(limit: int = 100, min_importance: int = 3):
    """
    Export recent important memories as JSON.

    Used by Claude Code to pull cross-session insights and write them
    to local memory markdown files — closing the sync loop.

    Returns memories ordered by recency, filtered by importance threshold.
    """
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.memory.vector_memory import export_memories
        memories = export_memories(limit=min(limit, 500), min_importance=min_importance)
        return {"memories": memories, "count": len(memories)}
    except Exception as e:
        raise HTTPException(500, f"Memory export failed: {e}")


@app.get("/memory/stats")
def memory_stats():
    """
    Show memory store statistics: total count, sources, last writes.
    Useful for confirming that cross-agent memory sync is working.
    """
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM agent_memories")
                total = cur.fetchone()[0]
                cur.execute("""
                    SELECT source, COUNT(*), MAX(created_at)
                    FROM agent_memories
                    GROUP BY source
                    ORDER BY COUNT(*) DESC
                """)
                by_source = [
                    {"source": r[0] or "unknown", "count": r[1],
                     "last_write": r[2].isoformat() if r[2] else None}
                    for r in cur.fetchall()
                ]
                cur.execute("""
                    SELECT COUNT(*) FROM agent_memories
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                """)
                last_24h = cur.fetchone()[0]
        return {
            "total_memories": total,
            "last_24h": last_24h,
            "by_source": by_source,
        }
    except Exception as e:
        # If source column doesn't exist yet (pre-migration), return basic stats
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM agent_memories")
                    total = cur.fetchone()[0]
            return {"total_memories": total, "note": str(e)}
        except Exception:
            raise HTTPException(500, f"Stats failed: {e}")


@app.get("/memory/search")
def memory_search(q: str, limit: int = 20, min_importance: int = 1, source: str | None = None):
    """
    Keyword search across the shared memory store.
    Lets any agent explicitly query the knowledge base rather than relying
    purely on passive injection at dispatch time.
    """
    if not q or len(q.strip()) < 2:
        raise HTTPException(400, "Query 'q' must be at least 2 characters")
    try:
        from app.memory.vector_memory import search_memories
        results = search_memories(q.strip(), limit=min(limit, 100),
                                  min_importance=min_importance, source=source)
        return {"query": q, "count": len(results), "results": results}
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


@app.post("/memory/prune")
def memory_prune(request: Request, days: int = 90, max_importance: int = 2):
    """
    Delete memories older than `days` with importance <= max_importance.
    Protected by MEMORY_INGEST_SECRET. Called weekly by the scheduler.
    """
    _secret = os.environ.get("MEMORY_INGEST_SECRET", "")
    if _secret and request.headers.get("X-Memory-Secret", "") != _secret:
        raise HTTPException(401, "Unauthorized")
    try:
        from app.memory.vector_memory import prune_old_memories, deduplicate_memories
        pruned = prune_old_memories(days=days, max_importance=max_importance)
        deduped = deduplicate_memories()
        _bg_log(f"memory/prune: removed {pruned} old + {deduped} duplicates", "memory")
        return {"pruned": pruned, "deduplicated": deduped}
    except Exception as e:
        raise HTTPException(500, f"Prune failed: {e}")


@app.post("/memory/n8n-ingest")
def memory_n8n_ingest(payload: dict, request: Request):
    """
    Receive workflow outcomes from n8n and store them as memories.
    n8n workflows (health monitor, verification monitor, etc.) call this
    after significant events so automation outcomes feed the shared KB.

    Payload: {"workflow": "...", "event": "...", "details": "...", "importance": 3}
    Protected by N8N_API_KEY header.
    """
    _key = os.environ.get("N8N_API_KEY", "")
    if _key and request.headers.get("X-N8N-Key", "") != _key:
        raise HTTPException(401, "Unauthorized")
    workflow = str(payload.get("workflow", "n8n"))
    event = str(payload.get("event", ""))
    details = str(payload.get("details", ""))
    importance = int(payload.get("importance", 3))
    if not event:
        raise HTTPException(400, "event field required")
    content = f"[n8n:{workflow}] {event}: {details}"[:800]
    try:
        from app.memory.vector_memory import ingest_external_memory
        ok = ingest_external_memory(content=content, memory_type="fact",
                                    importance=importance, source="n8n",
                                    session_id="n8n_automation")
        _bg_log(f"n8n memory ingest: {workflow}/{event} → {'ok' if ok else 'failed'}", "memory")
        return {"ok": ok, "stored": content[:80]}
    except Exception as e:
        raise HTTPException(500, f"n8n ingest failed: {e}")


@app.get("/memory/health")
def memory_health():
    """Detailed memory store health report — embedding coverage, growth rate, source breakdown."""
    try:
        from app.memory.vector_memory import memory_health_report
        return memory_health_report()
    except Exception as e:
        raise HTTPException(500, f"Health report failed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("cli_worker.main:app", host="0.0.0.0", port=8002, reload=False)
