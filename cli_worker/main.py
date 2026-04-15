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
from fastapi import FastAPI, HTTPException
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
    # Boot: create table, requeue stale tasks
    try:
        migrations.ensure_table()
        requeued = migrations.requeue_stale_tasks()
        if requeued:
            _bg_log(f"Boot: requeued {requeued} stale tasks (crash recovery)", "boot")
        else:
            _bg_log("Boot: DB ready, no stale tasks", "boot")
    except Exception as e:
        _bg_log(f"Boot DB error: {e} — task queue unavailable", "boot")

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
    _scheduler.start()
    _bg_log("CLI maintenance scheduler started (pro_keeper 15min, gemini_keeper 4h, claude_watchdog 90s, gemini_watchdog 90s, proactive_refresh 12h, cookie_keepalive 12h+6h offset)", "boot")

    # Boot-time: run watchdog immediately so CLI health is confirmed before the
    # first real request (normally would wait 90s). Run cookie keepalive too so
    # Layer 4 (cookie shortcut) is primed from the start, not after 6h.
    # Both run in the thread pool — non-blocking, fire-and-forget.
    _loop = asyncio.get_event_loop()
    _loop.run_in_executor(None, _pro_cli_watchdog_job)
    _loop.run_in_executor(None, _cookie_keepalive_job)
    _bg_log("Boot: watchdog + cookie keepalive fired immediately (no waiting for first interval)", "boot")

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
            ["claude", "-p", "ok", "--max-tokens", "3", "--output-format", "text"],
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "HOME": "/root"},
        )
        # Any non-empty stdout on returncode 0 means the prompt is alive
        if r.returncode == 0 and (r.stdout or b"").strip():
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
    result = {
        "status": "ok" if (claude_ok and db_ok) else "degraded",
        "claude_available": claude_ok,
        "gemini_available": gemini_ok,
        "db_connected": db_ok,
    }
    _health_cache = result
    _health_cache_ts = _time.time()
    return result


@app.post("/webhook/verification-code")
def receive_verification_code(request: dict):
    """
    Receive magic link URL (or 6-digit code) from n8n for automated Claude CLI re-login.
    Claude.ai sends a MAGIC LINK email — n8n extracts the URL and POSTs it here.

    The Playwright browser automation runs inside THIS container (VS-Code-inspiring-cat).
    The magic link URL must be placed in THIS process's _verification_code_queue so the
    waiting browser thread can navigate to it.
    """
    # Accept magic link URL (new) or legacy 6-digit code
    auth_payload = (
        str(request.get("url", "")).strip()
        or str(request.get("code", "")).strip()
    )
    if not auth_payload:
        return {"ok": False, "error": "No 'url' or 'code' field in payload"}

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("cli_worker.main:app", host="0.0.0.0", port=8002, reload=False)
