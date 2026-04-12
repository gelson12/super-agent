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
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg2
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import migrations, task_runner

# ── Scheduler (CLI maintenance jobs live here, not in API service) ────────────
_scheduler = AsyncIOScheduler(timezone="UTC")


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

    # Start background worker loop
    asyncio.create_task(_worker_loop())

    # Start CLI maintenance scheduler
    _scheduler.add_job(_pro_token_keeper_job, "interval", minutes=15,
                       id="pro_token_keeper", replace_existing=True)
    _scheduler.add_job(_gemini_token_keeper_job, "interval", hours=4,
                       id="gemini_token_keeper", replace_existing=True)
    _scheduler.add_job(_pro_cli_watchdog_job, "interval", minutes=3,
                       id="pro_cli_watchdog", replace_existing=True)
    _scheduler.start()
    _bg_log("CLI maintenance scheduler started (pro_keeper 15min, gemini_keeper 4h, watchdog 3min)", "boot")

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
    # Verify credentials file exists and contains auth data
    import json as _json
    creds_path = os.path.expanduser("/root/.gemini/credentials.json")
    try:
        with open(creds_path, "r") as f:
            creds = _json.load(f)
        # Must have either client credentials or an API key configured
        return bool(creds.get("client_id") or creds.get("api_key"))
    except Exception:
        # Also check settings.json for API key auth
        settings_path = os.path.expanduser("/root/.gemini/settings.json")
        try:
            with open(settings_path, "r") as f:
                settings = _json.load(f)
            return bool(settings.get("apiKey"))
        except Exception:
            return False


@app.get("/health")
def health():
    """
    Live health check — verifies Claude and Gemini CLIs are responding.
    Called by pro_cli_watchdog and pro_router.verify_pro_auth().
    """
    claude_ok  = _probe(["claude", "--version"])
    gemini_ok  = _gemini_auth_ok()
    db_ok = False
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if (claude_ok and db_ok) else "degraded",
        "claude_available": claude_ok,
        "gemini_available": gemini_ok,
        "db_connected": db_ok,
    }


@app.post("/webhook/verification-code")
def receive_verification_code(request: dict):
    """
    Receive a Claude.ai email verification code from n8n.
    The n8n workflow monitors the Hotmail inbox for Anthropic verification emails,
    extracts the code, and POSTs it here. The waiting Playwright thread picks it up.
    """
    code = request.get("code", "")
    if not code:
        return {"ok": False, "error": "No code provided"}
    try:
        import sys
        sys.path.insert(0, "/app")
        from app.learning.cli_auto_login import receive_verification_code as _recv
        _recv(code)
        _bg_log(f"Verification code received from n8n: {code[:2]}****", "webhook")
        return {"ok": True, "message": "Code received — auto-login proceeding"}
    except Exception as e:
        _bg_log(f"Verification code webhook error: {e}", "webhook")
        return {"ok": False, "error": str(e)}


@app.post("/tasks", status_code=201)
def submit_task(req: TaskSubmit):
    """
    Submit a CLI task. Returns task_id immediately.
    The background worker picks it up within 2 seconds.
    """
    valid_types = {"claude_pro", "gemini_cli", "claude_auth",
                   "claude_probe", "gemini_probe", "flutter_build"}
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
