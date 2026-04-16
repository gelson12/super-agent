"""
Super Agent — FastAPI backend
Endpoints:
  GET  /health              — liveness check
  POST /chat                — route message to best model
  POST /chat/direct         — force a specific model
  GET  /history/{sid}       — retrieve session history
  DELETE /history/{sid}     — clear session history
  GET  /credits             — API credit & usage status
  GET  /storage/status      — Cloudinary storage usage
  POST /storage/upload      — upload file with auto quota management
"""
import os
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import anthropic as _anthropic

from .routing.dispatcher import dispatch
from .memory.session import append_exchange, get_messages, clear_session
from .activity_log import bg_log, recent_lines as _activity_recent_lines, ACTIVITY_LOG
from .storage.cloudinary_manager import get_storage_status, upload_file
from .models.claude import ask_claude_vision
from .models.gemini import transcribe_audio
from .config import settings
from .cache.response_cache import cache
from .learning.insight_log import insight_log
from .learning.adapter import adapter
from .learning.wisdom_store import wisdom_store
from .learning.metrics_store import collect_current_snapshot, record_snapshot, get_trends, METRICS_PATH
from .learning.cost_ledger import record_call as _ledger_record, spend_summary as _spend_summary, get_breakdown as _cost_breakdown
from .learning.credit_throttle import should_run as _should_run, health_check_uses_llm as _hc_uses_llm, get_status as _throttle_status
# algorithm_store, benchmark, build_recipes imported lazily inside endpoints

limiter = Limiter(key_func=get_remote_address)

# ── Auth middleware ────────────────────────────────────────────────────────────
# Protected paths require header: X-Token: <UI_PASSWORD>
# If UI_PASSWORD is not set, auth is disabled.

_OPEN_PATHS = {"/", "/health", "/auth", "/credits/pro-status", "/credits/pro-reset", "/agents", "/dashboard", "/stats", "/stats/report"}
_OPEN_PREFIXES = ("/static", "/downloads", "/webhook", "/n8n/connection-info", "/activity", "/dashboard/", "/stats/")  # token-in-URL or public info


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.ui_password:
            return await call_next(request)
        path = request.url.path
        if path in _OPEN_PATHS or any(path.startswith(p) for p in _OPEN_PREFIXES):
            return await call_next(request)
        token = request.headers.get("X-Token", "")
        if not secrets.compare_digest(token, settings.ui_password):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


def _scheduled_health_check() -> None:
    """
    Runs on a dynamic interval (30min at full credit, 2hr at reduced, 4hr at minimal/critical).
    At CRITICAL credit tier: takes a metrics snapshot only — no LLM call.
    Throttled entirely only if credits are exhausted beyond all tiers (never for health).
    """
    try:
        # Always record a metrics snapshot — free, no LLM
        try:
            snap = collect_current_snapshot()
            record_snapshot(snap)
            trends = get_trends(hours=24)
            if trends.get("alert_count", 0) > 0:
                for alert in trends["alerts"]:
                    bg_log(f"TREND ALERT: {alert}", source="health_check")
            # Proactive anomaly alerting (email + activity log, 4h cooldown)
            try:
                from .learning.anomaly_alerter import check_and_alert as _check_alerts
                _fired = _check_alerts(snap)
                if _fired:
                    bg_log(f"Anomaly alerts fired: {_fired}", source="health_check")
            except Exception:
                pass
            # Verify actual Claude Pro auth state — syncs CLI_DOWN flag to reality
            # so the system never reports assumed/stale status
            try:
                from .learning.pro_router import verify_pro_auth as _verify_pro
                _auth = _verify_pro()
                if not _auth.get("pro_valid"):
                    bg_log(
                        f"Health check: Pro auth check FAILED — {_auth.get('message', '')}. "
                        "Falling back to ANTHROPIC_API_KEY until CLI recovers.",
                        source="health_check",
                    )
            except Exception:
                pass
            # Refresh CLI worker statuses — clears stale sick/strike states
            try:
                from .learning.agent_status_tracker import seed_live_status
                seed_live_status()
            except Exception:
                pass
        except Exception:
            pass

        # LLM-based investigation only when credit allows
        if not _hc_uses_llm():
            bg_log("Health check: metrics snapshot taken (LLM skipped — critical credit tier)", source="health_check")
            return

        from .agents.self_improve_agent import run_self_improve_agent
        bg_log("Scheduled health check starting — investigating all services autonomously", source="health_check")
        _t0 = __import__("time").time()
        run_self_improve_agent(
            "SCHEDULED HEALTH CHECK — investigate ALL services autonomously:\n"
            "1. railway_get_deployment_status + railway_get_logs — deployed and healthy?\n"
            "2. db_health_check + db_get_failure_patterns — DB healthy? Recurring errors?\n"
            "3. n8n_list_workflows — CALL THIS to verify n8n is LIVE and reachable.\n"
            "   If it returns data: report workflow count, how many active vs inactive.\n"
            "   If it returns an error: n8n is DOWN — report as CRITICAL.\n"
            "   DO NOT just check config files — you must verify the live instance.\n"
            "4. run_shell_command('supervisorctl status') — are all processes running?\n"
            "5. db_get_error_stats — which models/routes are failing most?\n"
            "Auto-fix any SAFE issues. Record findings in a brief internal log. "
            "Only notify the user if you find something critical that needs their input.\n"
            "IMPORTANT: Do NOT create test workflows during health checks. Only READ.",
            authorized=False,
        )
        _elapsed = __import__("time").time() - _t0
        # Record cost estimate: health check uses ~3000 input + ~1000 output chars on average
        _ledger_record("CLAUDE", 3000, 1000, category="health_check")
        bg_log(f"Scheduled health check complete ({_elapsed:.0f}s)", source="health_check")
    except Exception as _e:
        bg_log(f"Scheduled health check error: {_e}", source="health_check")


def _post_deploy_check() -> None:
    """
    Runs once immediately after startup — verifies the fresh deploy is healthy.
    Regenerates stale APK download links, checks all env vars are present.
    """
    import time as _t
    _t.sleep(15)  # wait for the app to fully bind
    try:
        from .agents.self_improve_agent import run_self_improve_agent
        bg_log("Post-deploy startup check starting — verifying fresh deploy is healthy", source="post_deploy")
        # Run n8n health check separately (fast, no LLM call)
        try:
            from .tools.n8n_repair import monitor_n8n
            bg_log("Post-deploy: running n8n health check", source="post_deploy")
            monitor_n8n()
        except Exception:
            pass

        # Auto-create Claude Verification Code Monitor workflow if not exists
        try:
            from .tools.n8n_tools import n8n_list_workflows, n8n_create_workflow
            import json as _json
            _wf_list = n8n_list_workflows.invoke({})
            if "Claude Verification Code Monitor" not in _wf_list:
                _wf_json_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "n8n", "claude_verification_monitor.json")
                if os.path.isfile(_wf_json_path):
                    with open(_wf_json_path) as _f:
                        _wf_data = _json.load(_f)
                    _nodes = _json.dumps(_wf_data.get("nodes", []))
                    _conns = _json.dumps(_wf_data.get("connections", {}))
                    _result = n8n_create_workflow.invoke({
                        "name": "Claude Verification Code Monitor",
                        "nodes_json": _nodes,
                        "connections_json": _conns,
                    })
                    bg_log(f"Post-deploy: created Claude Verification Code Monitor workflow in n8n: {str(_result)[:200]}", source="post_deploy")
                else:
                    bg_log("Post-deploy: claude_verification_monitor.json not found in repo", source="post_deploy")
            else:
                bg_log("Post-deploy: Claude Verification Code Monitor workflow already exists in n8n", source="post_deploy")
        except Exception as _e:
            bg_log(f"Post-deploy: n8n workflow auto-create skipped — {_e}", source="post_deploy")

        run_self_improve_agent(
            "POST-DEPLOY STARTUP CHECK — this container just started. Do ALL of:\n"
            "1. railway_get_deployment_status — confirm this deploy succeeded\n"
            "2. railway_list_variables — verify RAILWAY_PUBLIC_DOMAIN, GITHUB_PAT, "
            "CLOUDINARY_*, ANTHROPIC_API_KEY, N8N_BASE_URL, N8N_API_KEY are all set\n"
            "3. db_health_check — is the database reachable?\n"
            "4. Check if /workspace/apk_downloads exists and regenerate download links "
            "if any APKs are present (redeploys invalidate previous Railway-served links)\n"
            "5. If any required env var is missing, report it clearly.\n"
            "Be concise — this runs silently in the background.",
            authorized=False,
        )
        _ledger_record("CLAUDE", 2000, 800, category="health_check")
        bg_log("Post-deploy startup check complete", source="post_deploy")
    except Exception as _e:
        bg_log(f"Post-deploy check error: {_e}", source="post_deploy")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from apscheduler.schedulers.background import BackgroundScheduler
    from .learning.nightly_review import run_nightly_review
    from .learning.weekly_review import run_weekly_review
    from .learning.improvement_monitor import tick as _monitor_tick
    import threading as _threading
    # Run post-deploy check in a background thread (not the scheduler)
    # so it fires once on every startup without blocking the lifespan.
    _threading.Thread(target=_post_deploy_check, daemon=True).start()

    # Refresh Pro token on startup — ensures Railway var is always current
    # so the NEXT redeploy gets a fresh token regardless of when it happens.
    def _startup_token_refresh():
        try:
            from .learning.pro_token_keeper import run_token_keeper
            run_token_keeper()
        except Exception:
            pass
    _threading.Thread(target=_startup_token_refresh, daemon=True).start()

    # Bootstrap prompt library — seeds v1 for all tracked prompts from static constants.
    # Best-effort: never delays startup if it fails.
    try:
        from .learning.prompt_library import prompt_library as _pl
        from . import prompts as _prompts_module
        _pl.bootstrap(_prompts_module)
    except Exception:
        pass

    # Validate session DB is reachable — log result so silent failures are visible
    def _validate_session_db():
        try:
            from .memory.session import DB_PATH, get_session_history
            test = get_session_history("__startup_test__")
            _ = test.messages  # Force connection
            bg_log(f"Session DB OK: {DB_PATH[:80]}", source="startup")
        except Exception as e:
            bg_log(f"SESSION DB FAILED at startup: {e} — session memory unavailable", source="startup")
    _threading.Thread(target=_validate_session_db, daemon=True).start()

    # Seed agent status tracker from insight log so dashboard doesn't show all sleeping
    try:
        from .learning.agent_status_tracker import seed_from_insight_log, seed_live_status
        seed_from_insight_log()
        # Proactively check API credits + CLI health so dashboard reflects
        # reality immediately (e.g. Anthropic shows "strike" if no credits).
        # Delay 30s to let CLI worker finish booting — avoids false "sick" on startup.
        import threading as _seed_t
        def _delayed_seed():
            import time as _t
            _t.sleep(30)
            seed_live_status()
        _seed_t.Thread(target=_delayed_seed, daemon=True).start()
    except Exception:
        pass

    scheduler = BackgroundScheduler()
    # Health check: starts at 30min, dynamically respected via _hc_uses_llm() inside the job.
    # The interval itself stays at 30min but the job skips the LLM at reduced/critical tiers.
    # This avoids needing to restart the scheduler when credit tier changes.
    scheduler.add_job(
        _scheduled_health_check,
        "interval",
        minutes=30,
        id="health_check",
        replace_existing=True,
    )
    def _nightly_review_job():
        if not _should_run("nightly_review"):
            return
        run_nightly_review()
        _ledger_record("CLAUDE", 5000, 3000, category="code_review")

    def _weekly_review_job():
        if not _should_run("weekly_review"):
            return
        run_weekly_review()
        _ledger_record("OPUS", 8000, 6000, category="improvement")

    scheduler.add_job(
        _nightly_review_job,
        "cron",
        hour=23,
        minute=0,
        id="nightly_review",
        replace_existing=True,
    )
    scheduler.add_job(
        _weekly_review_job,
        "cron",
        day_of_week="sun",
        hour=23,
        minute=0,
        id="weekly_review",
        replace_existing=True,
    )
    scheduler.add_job(
        _monitor_tick,
        "interval",
        minutes=30,
        id="improvement_monitor",
        replace_existing=True,
    )

    # Weekly benchmark — runs every Monday 01:00 UTC (day after weekly review)
    def _benchmark_job():
        if not _should_run("benchmark"):
            bg_log("Benchmark skipped — credit throttle active", source="benchmark")
            return
        try:
            from .learning.benchmark import run_benchmark
            run_benchmark()
            _ledger_record("HAIKU", 10000, 5000, category="benchmark")
        except Exception as _e:
            bg_log(f"Benchmark job error: {_e}", source="benchmark")

    scheduler.add_job(
        _benchmark_job,
        "cron",
        day_of_week="mon",
        hour=1,
        minute=0,
        id="benchmark",
        replace_existing=True,
    )

    # NOTE: Pro token keeper, Gemini token keeper, and Pro CLI watchdog jobs
    # have been moved to cli_worker/main.py — they run inside the dedicated
    # CLI worker Railway service which has direct access to the Claude/Gemini CLIs.
    # The API service no longer needs to manage CLI credentials.

    # n8n autonomous monitor — checks every 15 minutes, auto-repairs detected issues
    def _n8n_monitor_job():
        try:
            from .tools.n8n_repair import monitor_n8n
            bg_log("n8n monitor: running automated health check", source="n8n_monitor")
            monitor_n8n()
        except Exception as _e:
            bg_log(f"n8n monitor error: {_e}", source="n8n_monitor")

    scheduler.add_job(
        _n8n_monitor_job,
        "interval",
        minutes=15,
        id="n8n_monitor",
        replace_existing=True,
    )

    scheduler.start()
    yield
    # Flush insight log on shutdown so activity history survives redeploys
    try:
        from .learning.insight_log import insight_log as _il
        _il._flush()
    except Exception:
        pass
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Super Agent Backend",
    description="Multi-model AI agent with semantic routing (Claude / Gemini / DeepSeek)",
    version="1.0.0",
    lifespan=_lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(AuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)  # 8000 to accommodate [APP_CONTEXT] blocks
    session_id: str = Field(default="default", max_length=128)


class DirectChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    model: str = Field(..., description="GEMINI | DEEPSEEK | CLAUDE")
    session_id: str = Field(default="default", max_length=128)


class ChatResponse(BaseModel):
    response: str
    model_used: str
    routed_by: str
    session_id: str
    complexity: int = 0
    cache_hit: bool = False
    # Collective intelligence fields (additive — backwards compatible)
    was_reviewed: bool = False
    is_ensemble: bool = False
    cot_used: bool = False
    red_team_ran: bool = False
    escalated: bool = False
    confidence_score: int = 100
    cloudinary_url: str | None = None


class EnsembleRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: str = Field(default="default", max_length=128)


class HistoryMessage(BaseModel):
    role: str
    content: str


class UploadRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to file to upload")
    resource_type: str = Field(default="auto", description="image | video | raw | auto")
    public_id: str = Field(default=None, description="Optional custom public ID")


# ── Static UI ─────────────────────────────────────────────────────────────────

_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

@app.get("/", include_in_schema=False)
def root():
    index = os.path.join(_static_dir, "index.html")
    return FileResponse(index)


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    """Serve the improvement reports dashboard."""
    path = os.path.join(_static_dir, "dashboard.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return {"error": "dashboard.html not found in static/"}


@app.get("/agents", include_in_schema=False)
def agents_page():
    """Serve the visual agents office dashboard."""
    path = os.path.join(_static_dir, "agents.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return {"error": "agents.html not found in static/"}


@app.get("/dashboard/agents/status", tags=["meta"])
def agents_status():
    """
    Real-time status of all models and agents for the visual dashboard.
    Enriched with Pro subscription flags for Claude CLI Pro so the UI can show
    burst/daily/cli_down states without a separate API call.
    """
    from .learning.agent_status_tracker import get_all_statuses
    workers = get_all_statuses()

    # Inject Pro subscription state into Claude CLI Pro worker (instant — reads flag files only)
    try:
        from .learning.pro_router import get_status as _pro_status
        pro = _pro_status()
        for w in workers:
            if w["id"] == "Claude CLI Pro":
                w["pro_mode"]      = pro.get("mode", "pro_primary")
                w["pro_flags"]     = pro.get("flags", {})
                w["pro_resets_in"] = pro.get("resets_in", {})
                w["pro_message"]   = pro.get("message", "")
                break
    except Exception:
        pass  # Non-fatal — dashboard degrades gracefully without Pro flags

    # Inject login rate-limit status for Claude CLI Pro (reads flag file — no network I/O)
    try:
        from .learning.cli_auto_login import get_login_ratelimit_status as _rl_status
        rl = _rl_status()
        if rl.get("blocked"):
            for w in workers:
                if w["id"] == "Claude CLI Pro":
                    w["login_ratelimit_remaining_s"] = rl["remaining_s"]
                    w["login_ratelimit_hit_count"]   = rl["hit_count"]
                    break
    except Exception:
        pass  # Non-fatal

    # Inject CLI timeout count for Claude CLI Pro (reads counter file — no network I/O)
    try:
        from .learning.pro_router import get_cli_timeout_count as _timeout_count
        count = _timeout_count()
        if count > 0:
            for w in workers:
                if w["id"] == "Claude CLI Pro":
                    w["cli_timeout_count"] = count
                    break
    except Exception:
        pass  # Non-fatal

    return {"workers": workers}


@app.get("/dashboard/agents/{worker_id}/history", tags=["meta"])
def agent_history(worker_id: str):
    """Activity history for a specific worker — last 20 interactions."""
    from .learning.agent_status_tracker import get_worker_history
    # URL-decode worker_id (spaces come as %20)
    import urllib.parse
    decoded = urllib.parse.unquote(worker_id)
    history = get_worker_history(decoded, limit=20)
    return {"worker": decoded, "history": history}


@app.get("/dashboard/agents/{worker_id}/log", tags=["meta"])
def agent_log(worker_id: str, limit: int = 60):
    """
    Combined activity log for a specific worker — merged from two sources:
      1. Per-agent state-change events (mark_working/done/sick/strike/talking),
         written to /workspace/agent_logs/<name>.jsonl on every state change.
      2. Insight log records — every user request this worker handled.
    Returns newest-first, capped at `limit` entries.
    """
    import urllib.parse
    from datetime import datetime, timezone
    decoded = urllib.parse.unquote(worker_id)
    events: list[dict] = []

    # ── Source 1: per-agent state-change event log ─────────────────────────
    try:
        from .learning.agent_status_tracker import read_agent_log
        state_events = read_agent_log(decoded, limit=limit)
        for e in state_events:
            events.append({
                "ts":     e.get("ts", 0),
                "type":   "state",
                "event":  e.get("event", ""),
                "detail": e.get("detail", ""),
            })
    except Exception:
        pass

    # ── Source 2: PostgreSQL agent_activity (persistent across restarts) ────
    try:
        from .learning.agent_status_tracker import get_db_activity
        db_events = get_db_activity(decoded, limit=limit)
        for e in db_events:
            events.append({
                "ts":     e.get("ts", 0),
                "type":   "state",
                "event":  e.get("event", ""),
                "detail": e.get("detail", ""),
            })
    except Exception:
        pass

    # ── Source 3: insight log (dispatched user requests) ──────────────────
    try:
        from .learning.agent_status_tracker import get_worker_history
        for h in get_worker_history(decoded, limit=limit):
            ts = h.get("ts", 0)
            complexity = h.get("complexity", 0)
            route = h.get("routed_by", "")
            words = h.get("msg_words", 0)
            resp = h.get("resp_len", 0)
            err = h.get("error", False)
            detail = (
                f"route={route} complexity={'★'*complexity} "
                f"words={words} resp={resp}chars"
            )
            events.append({
                "ts":     ts,
                "type":   "request",
                "event":  "error" if err else "request",
                "detail": detail,
                "model":  h.get("model", ""),
                "error":  err,
            })
    except Exception:
        pass

    # Sort newest-first, deduplicate by (ts, event)
    events.sort(key=lambda x: x.get("ts", 0), reverse=True)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for e in events:
        key = (round(e.get("ts", 0)), e.get("event", ""), e.get("detail", "")[:30])
        if key not in seen:
            seen.add(key)
            # Add human-readable timestamp
            ts = e.get("ts", 0)
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                e["date"] = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                e["date"] = ""
            unique.append(e)
        if len(unique) >= limit:
            break

    return {"worker": decoded, "events": unique, "total": len(unique)}


@app.get("/dashboard/agents/{worker_id}/db-activity", tags=["meta"])
def agent_db_activity(worker_id: str, limit: int = 60):
    """
    Persistent activity log for a worker from the PostgreSQL agent_activity table.
    Unlike the JSONL log, this survives container restarts and spans the full history.
    """
    import urllib.parse
    decoded = urllib.parse.unquote(worker_id)
    from .learning.agent_status_tracker import get_db_activity
    rows = get_db_activity(decoded, limit=limit)
    return {"worker": decoded, "events": rows, "total": len(rows)}


@app.get("/dashboard/agents/interactions", tags=["meta"])
def agent_interactions(limit: int = 100):
    """
    Recent interactions between agents — shows all talking/collaboration events
    stored in the PostgreSQL agent_interactions table. Newest first.
    """
    from .learning.agent_status_tracker import get_db_interactions
    rows = get_db_interactions(limit=limit)
    return {"interactions": rows, "total": len(rows)}


@app.get("/dashboard/n8n/workflows", tags=["meta"])
def dashboard_n8n_workflows():
    """
    Return all n8n workflows as structured JSON split into active (working) and
    inactive (non-working) lists. Used by the office panel 'View Workflows' popup.
    Each workflow has: id, name, active, updatedAt, lastRunAt, lastRunStatus.
    """
    try:
        from .tools.n8n_tools import _check_config, _get
        err = _check_config()
        if err:
            return {"error": err, "active": [], "inactive": [], "total": 0}

        all_rows = []
        cursor = None
        while True:
            path = "/api/v1/workflows?limit=250"
            if cursor:
                path += f"&cursor={cursor}"
            result = _get(path)
            if isinstance(result, str):
                return {"error": result, "active": [], "inactive": [], "total": 0}
            rows = result.get("data", [])
            all_rows.extend(rows)
            cursor = result.get("nextCursor")
            if not cursor:
                break

        active   = []
        inactive = []
        for w in all_rows:
            entry = {
                "id":            w.get("id", ""),
                "name":          w.get("name", "Unnamed"),
                "active":        bool(w.get("active", False)),
                "updatedAt":     (w.get("updatedAt") or w.get("updated_at") or "")[:19],
                "tags":          [t.get("name", "") for t in (w.get("tags") or [])],
                "lastRunAt":     "",   # filled below from executions API
                "lastRunStatus": "",   # success | error | crashed | waiting | running
            }
            (active if entry["active"] else inactive).append(entry)

        active.sort(key=lambda x: x["name"].lower())
        inactive.sort(key=lambda x: x["name"].lower())

        # Enrich with last-execution data (single API call, newest-first).
        # Covers all workflows that have run in the most recent 250 executions.
        # Non-fatal — if the executions endpoint is unavailable the dashboard
        # still works, just without last-run timestamps.
        try:
            exec_result = _get("/api/v1/executions?limit=250&includeData=false")
            if isinstance(exec_result, dict):
                execution_map: dict[str, dict] = {}
                for ex in (exec_result.get("data") or []):
                    # n8n v1 API: workflowId is top-level; fall back to nested path
                    wf_id = str(
                        ex.get("workflowId")
                        or (ex.get("data") or {}).get("workflowData", {}).get("id", "")
                        or ""
                    )
                    if wf_id and wf_id not in execution_map:
                        # First match = most recent (API returns newest-first)
                        execution_map[wf_id] = {
                            "lastRunAt":     (ex.get("startedAt") or ex.get("stoppedAt") or ""),
                            "lastRunStatus": ex.get("status", ""),
                        }
                for entry in active + inactive:
                    ex = execution_map.get(str(entry["id"]))
                    if ex:
                        entry["lastRunAt"]     = (ex["lastRunAt"]     or "")[:24]
                        entry["lastRunStatus"] = (ex["lastRunStatus"] or "")[:20]
        except Exception:
            pass  # non-fatal

        return {
            "active":   active,
            "inactive": inactive,
            "total":    len(all_rows),
        }
    except Exception as e:
        return {"error": str(e), "active": [], "inactive": [], "total": 0}


@app.get("/debug/talking-test", tags=["meta"])
def debug_talking_test(seconds: int = 30):
    """
    Force two workers into 'talking' state for N seconds (default 30).
    Use this to visually verify the talking-lines animation on the dashboard.
    E.g. GET /debug/talking-test?seconds=60
    """
    import threading
    from .learning.agent_status_tracker import mark_talking, clear_talking
    worker_a = "Sonnet Anthropic"
    worker_b = "Claude CLI Pro"
    mark_talking(worker_a, worker_b)

    def _clear_later():
        import time as _t
        _t.sleep(max(5, min(seconds, 300)))
        clear_talking(worker_a, worker_b)

    threading.Thread(target=_clear_later, daemon=True).start()
    return {
        "status": "ok",
        "message": f"'{worker_a}' ↔ '{worker_b}' set to talking for {seconds}s — open /agents to verify",
    }


@app.post("/debug/n8n-cleanup", tags=["meta"])
def debug_n8n_cleanup():
    """
    Directly invoke the n8n_cleanup_test_workflows tool — deletes junk/test
    workflows from n8n while protecting production ones.
    Protected: 'super agent chat', 'business hub', 'daily report', etc.
    """
    try:
        from .tools.n8n_tools import n8n_cleanup_test_workflows
        result = n8n_cleanup_test_workflows.invoke({})
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────

def _check_vault_mcp() -> bool:
    """Ping vault MCP server — returns True if reachable."""
    try:
        import httpx
        r = httpx.get(
            "http://obsidian-vault.railway.internal:22360/sse",
            timeout=3,
            headers={"Accept": "text/event-stream"},
        )
        return r.status_code < 500
    except Exception:
        return False


@app.get("/health", tags=["meta"])
def health():
    try:
        from .learning.pro_router import is_pro_available, is_cli_down
        _pro = is_pro_available()
        _cli_down = is_cli_down()
    except Exception:
        _pro, _cli_down = False, True
    _api = bool(settings.anthropic_api_key)
    _gemini = bool(
        getattr(settings, "gemini_api_key", None)
        or os.environ.get("GEMINI_SESSION_TOKEN")
    )
    _vault = _check_vault_mcp()
    if not _vault:
        try:
            from .alerts.notifier import alert_vault_unreachable
            alert_vault_unreachable("health check ping failed")
        except Exception:
            pass
    _status = "healthy" if _pro else ("degraded" if (_api or _gemini) else "critical")
    return {
        "ok": True,
        "version": "1.0.0",
        "status": _status,
        "pro_cli_available": _pro,
        "cli_down_flag": _cli_down,
        "gemini_available": _gemini,
        "api_key_available": _api,
        "vault_mcp_available": _vault,
    }


class AuthRequest(BaseModel):
    password: str


@app.post("/auth", tags=["meta"])
def auth(req: AuthRequest):
    """Validate password. Returns ok:true if correct."""
    if not settings.ui_password:
        return {"ok": True}
    if not secrets.compare_digest(req.password, settings.ui_password):
        raise HTTPException(status_code=401, detail="Wrong password")
    return {"ok": True}


def _chat_response_from_result(result: dict, session_id: str) -> ChatResponse:
    """Build a ChatResponse from a dispatch result dict."""
    return ChatResponse(
        response=result["response"],
        model_used=result["model_used"] or "UNKNOWN",
        routed_by=result["routed_by"],
        session_id=session_id,
        complexity=result.get("complexity", 0),
        cache_hit=result.get("cache_hit", False),
        was_reviewed=result.get("was_reviewed", False),
        is_ensemble=result.get("is_ensemble", False),
        cot_used=result.get("cot_used", False),
        red_team_ran=result.get("red_team_ran", False),
        escalated=result.get("escalated", False),
        confidence_score=result.get("confidence_score", 100),
        cloudinary_url=result.get("cloudinary_url"),
    )


@app.post("/chat", response_model=ChatResponse, tags=["agent"])
@limiter.limit("30/minute")
def chat(req: ChatRequest, request: Request):
    """Auto-route message to best model via semantic classifier."""
    try:
        result = dispatch(req.message, session_id=req.session_id)
    except Exception as _e:
        # Never crash — return a graceful error response
        bg_log(f"Dispatch crash caught: {_e}", source="chat_endpoint")
        result = {
            "model_used": "ERROR",
            "response": (
                "All models are currently unavailable. "
                "Claude CLI Pro may need a token refresh, and Anthropic API may have no credits. "
                "The system will auto-recover — please try again in a few minutes."
            ),
            "routed_by": "error_handler",
            "complexity": 0,
            "cache_hit": False,
        }
    if not result["response"].startswith("["):
        append_exchange(req.session_id, req.message, result["response"])
        _cat = "n8n_workflow" if req.session_id.startswith("n8n") else "chat"
        _ledger_record(result.get("model_used", "UNKNOWN"), len(req.message), len(result["response"]), category=_cat)
    return _chat_response_from_result(result, req.session_id)


@app.post("/chat/stream", tags=["agent"])
@limiter.limit("30/minute")
def chat_stream(req: ChatRequest, request: Request):
    """
    Stream endpoint with full dispatcher routing.

    - Action requests (GitHub, shell, Flutter/APK builds, n8n, self-improve,
      debug) are routed through the same dispatcher as POST /chat.
      Their full response is then SSE-streamed as chunks.
    - Conversational requests stream token-by-token directly from Claude
      with the proper Super Agent system prompt.
    """
    from .routing.dispatcher import (
        dispatch,
        _is_github_request,
        _is_shell_request,
        _is_n8n_request,
        _is_self_improve_request,
        _is_debug_request,
        _is_search_request,
    )
    from .prompts import SYSTEM_PROMPT_CLAUDE, build_capabilities_block
    from .learning.adapter import adapter as _adapter

    _SSE_HEADERS = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }

    # Detect whether this message needs tool routing
    msg = req.message
    _needs_routing = (
        _is_github_request(msg)
        or _is_shell_request(msg)
        or _is_n8n_request(msg)
        or _is_self_improve_request(msg)
        or _is_debug_request(msg)
        or _is_search_request(msg)
    )

    if _needs_routing:
        # Emit progress events IMMEDIATELY, then run dispatch inside the generator
        # so the user sees "thinking" feedback instead of a frozen cursor.
        def _generate_routed():
            # ── 1. Emit a context-aware progress message with time estimate ───
            _msg_lower = msg.lower()
            _is_apk = any(w in _msg_lower for w in ("apk", "flutter", "android app", "build app", "voice app"))
            if _is_n8n_request(msg):
                yield "data: [PROGRESS:⚙️ Building n8n workflow… (est. 1–4 min)]\n\n"
            elif _is_github_request(msg):
                yield "data: [PROGRESS:📁 Accessing GitHub… (est. 20–60s)]\n\n"
            elif _is_shell_request(msg):
                if _is_apk:
                    yield "data: [PROGRESS:💻 Compiling Android APK… (est. 5–15 min)]\n\n"
                else:
                    yield "data: [PROGRESS:💻 Running shell commands… (est. 30–90s)]\n\n"
            elif _is_self_improve_request(msg):
                yield "data: [PROGRESS:🔬 Analysing system health… (est. 1–3 min)]\n\n"
            elif _is_debug_request(msg):
                yield "data: [PROGRESS:🐛 Running diagnostics… (est. 30–90s)]\n\n"
            elif _is_search_request(msg):
                yield "data: [PROGRESS:🔍 Searching the web… (est. 10–20s)]\n\n"
            else:
                yield "data: [PROGRESS:🤖 Routing request… (est. 10–30s)]\n\n"

            # ── 2. Run the full dispatcher (agents, tools, routing) ───────────
            # Clear the per-thread API usage flag before dispatch so we can
            # detect if any ask_claude* call fell through to the Anthropic API.
            try:
                from .models.claude import clear_api_fallback_flag, api_fallback_used
                clear_api_fallback_flag()
            except Exception:
                api_fallback_used = lambda: False  # noqa: E731
            # Also clear the pro_router progress queue so stale events don't bleed in
            try:
                from .learning.pro_router import drain_progress_events as _drain
                _drain()  # clear any leftover events from a previous request
            except Exception:
                _drain = lambda: []  # noqa: E731
            try:
                _result = dispatch(msg, session_id=req.session_id)
            except Exception as _dispatch_err:
                err = str(_dispatch_err)[:300].replace(chr(10), " ")
                yield f"data: Agent encountered an error: {err}\n\n"
                yield "data: [META:ERROR·dispatch_error·0]\n\n"
                yield "data: [DONE]\n\n"
                return

            _response_text = _result["response"]

            # ── 2b. Drain self-healing / retry events queued by pro_router ────
            # These capture timeout retries and self-heal milestones so the user
            # sees exactly what Super Agent did on their behalf.
            try:
                from .learning.pro_router import drain_progress_events
                for _ev in drain_progress_events():
                    yield f"data: [PROGRESS:{_ev}]\n\n"
            except Exception:
                pass

            # ── API fallback detection ────────────────────────────────────────
            # Two signals: sentinel prefix from agent functions (tool-using agents)
            # OR thread-local flag from ask_claude* (conversational/search routes).
            _API_MARKER = "\x00API_FALLBACK\x00"
            _api_warned = False
            if _response_text.startswith(_API_MARKER):
                _response_text = _response_text[len(_API_MARKER):]
                yield "data: [PROGRESS:⚠️ Claude CLI & Gemini unavailable — Anthropic API used (costs credits)]\n\n"
                _api_warned = True
            if not _api_warned:
                try:
                    if api_fallback_used():
                        yield "data: [PROGRESS:⚠️ Claude CLI & Gemini unavailable — Anthropic API used (costs credits)]\n\n"
                except Exception:
                    pass

            if not _response_text.startswith("["):
                append_exchange(req.session_id, msg, _response_text)
                _cat = "n8n_workflow" if req.session_id.startswith("n8n") else "chat"
                _ledger_record(_result.get("model_used", "UNKNOWN"), len(msg), len(_response_text), category=_cat)

            # ── 3. Stream the response in word-boundary chunks ────────────────
            # Normalize: replace bare newlines with space+newline+space so that
            # words on adjacent lines don't merge (e.g. "This\nis" → "This \n is").
            normalized = _response_text.replace("\n", " \n ")
            words = [w for w in normalized.split(" ") if w]
            buf = ""
            for word in words:
                buf += word + " "
                if len(buf) >= 60:
                    chunk = buf.replace(chr(10), chr(92) + "n")
                    yield f"data: {chunk}\n\n"
                    buf = ""
            if buf.strip():
                yield f"data: {buf.replace(chr(10), chr(92) + 'n')}\n\n"
            _model = _result.get("model_used", "AGENT")
            _route = _result.get("routed_by", "dispatcher")
            _mem = _result.get("memory_count", 0)
            yield f"data: [META:{_model}·{_route}·{_mem}]\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_generate_routed(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # Conversational — CLI first (free), Anthropic API as last resort
    from .memory.vector_memory import get_memory_context as _get_mem, store_memory as _store_mem
    _caps = build_capabilities_block(settings)
    try:
        _mem_ctx = _get_mem(msg)
    except Exception:
        _mem_ctx = None
    _learned = _adapter.get_learned_context() or ""
    if _mem_ctx:
        _learned = _mem_ctx + "\n" + _learned
    # Use .replace() instead of .format() so that {curly braces} inside
    # stored memory (e.g. n8n JSON) don't cause a KeyError in .format().
    system = SYSTEM_PROMPT_CLAUDE.replace("{capabilities}", _caps).replace("{learned_context}", _learned)
    _mem_count = _mem_ctx.count("\n-") if _mem_ctx else 0

    def _generate():
        from .learning.agent_status_tracker import mark_working as _mw, mark_done as _md, mark_error as _me
        from .learning.insight_log import insight_log as _il

        # Simple complexity estimate based on word count (no classifier in conv path)
        _cx = min(5, max(1, len(msg.split()) // 15 + 1))

        # ── 1. Claude CLI (zero cost) ─────────────────────────────────────────
        yield "data: [PROGRESS:🤔 Thinking… (est. 5–15s)]\n\n"
        try:
            from .learning.pro_router import try_pro, should_attempt_cli, drain_progress_events as _drain_conv
            if should_attempt_cli():
                _mw("Claude CLI Pro", msg[:100])
                yield "data: [PROGRESS:⚡ Using Claude CLI… (est. 5–15s)]\n\n"
                cli_resp = try_pro(f"{system}\n\n{msg}")
                _md("Claude CLI Pro")
                # Surface any self-healing events that fired during the CLI call
                for _ev in _drain_conv():
                    yield f"data: [PROGRESS:{_ev}]\n\n"
                if cli_resp and (cli_resp.startswith("[") or cli_resp.lstrip().startswith('{"type":"error"')):
                    _me("Claude CLI Pro", cli_resp[:200])
                if cli_resp and not cli_resp.startswith("[") and not cli_resp.lstrip().startswith('{"type":"error"'):
                    _store_mem(req.session_id, f"Q: {msg[:300]} A: {cli_resp[:300]}")
                    append_exchange(req.session_id, msg, cli_resp)
                    _il.record(msg, "CLAUDE", cli_resp, "conversational", _cx, req.session_id)
                    normalized = cli_resp.replace("\n", " \n ")
                    words = [w for w in normalized.split(" ") if w]
                    buf = ""
                    for word in words:
                        buf += word + " "
                        if len(buf) >= 60:
                            yield f"data: {buf.replace(chr(10), chr(92) + 'n')}\n\n"
                            buf = ""
                    if buf.strip():
                        yield f"data: {buf.replace(chr(10), chr(92) + 'n')}\n\n"
                    yield f"data: [META:CLI·conversational·{_mem_count}]\n\n"
                    yield "data: [DONE]\n\n"
                    return
        except Exception:
            try: _md("Claude CLI Pro")
            except Exception: pass

        # ── 2. Gemini CLI (free fallback) ─────────────────────────────────────
        yield "data: [PROGRESS:🤖 Trying Gemini… (est. 5–10s)]\n\n"
        try:
            from .learning.gemini_cli_worker import ask_gemini_cli
            _mw("Gemini CLI", msg[:100])
            gemini_resp = ask_gemini_cli(msg)
            _md("Gemini CLI")
            if gemini_resp and gemini_resp.startswith("["):
                _me("Gemini CLI", gemini_resp[:200])
            if gemini_resp and not gemini_resp.startswith("["):
                _store_mem(req.session_id, f"Q: {msg[:300]} A: {gemini_resp[:300]}")
                append_exchange(req.session_id, msg, gemini_resp)
                _il.record(msg, "GEMINI_CLI", gemini_resp, "conversational", _cx, req.session_id)
                normalized = gemini_resp.replace("\n", " \n ")
                words = [w for w in normalized.split(" ") if w]
                buf = ""
                for word in words:
                    buf += word + " "
                    if len(buf) >= 60:
                        yield f"data: {buf.replace(chr(10), chr(92) + 'n')}\n\n"
                        buf = ""
                if buf.strip():
                    yield f"data: {buf.replace(chr(10), chr(92) + 'n')}\n\n"
                yield f"data: [META:GEMINI·conversational·{_mem_count}]\n\n"
                yield "data: [DONE]\n\n"
                return
        except Exception:
            try: _md("Gemini CLI")
            except Exception: pass

        # ── 3. Anthropic API ──────────────────────────────────────────────────
        yield "data: [PROGRESS:🔄 Trying Anthropic API…]\n\n"
        _api_ok = False
        try:
            _mw("Sonnet Anthropic", msg[:100])
            _client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)
            with _client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": msg}],
            ) as _stream:
                _buf = ""
                _full_api_resp = ""
                for _token in _stream.text_stream:
                    _full_api_resp += _token
                    _buf += _token
                    if len(_buf) >= 60:
                        yield f"data: {_buf.replace(chr(10), chr(92) + 'n')}\n\n"
                        _buf = ""
                if _buf.strip():
                    yield f"data: {_buf.replace(chr(10), chr(92) + 'n')}\n\n"
            _md("Sonnet Anthropic")
            if _full_api_resp:
                _store_mem(req.session_id, f"Q: {msg[:300]} A: {_full_api_resp[:300]}")
                append_exchange(req.session_id, msg, _full_api_resp)
                _il.record(msg, "SONNET", _full_api_resp, "conversational_api", _cx, req.session_id)
                _api_ok = True
            if _api_ok:
                yield f"data: [META:ANTHROPIC·conversational·{_mem_count}]\n\n"
                yield "data: [DONE]\n\n"
                return
        except Exception as _api_exc:
            try:
                _md("Sonnet Anthropic")
                _no_credit = ("credit balance", "insufficient", "payment required", "no credits")
                if any(p in str(_api_exc).lower() for p in _no_credit):
                    from .learning.agent_status_tracker import mark_strike as _ms
                    _ms("Sonnet Anthropic")
                else:
                    _me("Sonnet Anthropic", str(_api_exc)[:200])
            except Exception: pass

        # ── 4. DeepSeek (last resort) ─────────────────────────────────────────
        yield "data: [PROGRESS:🔄 Trying DeepSeek (last resort)…]\n\n"
        _ds_resp = None
        try:
            from .models.deepseek import ask_deepseek
            if not settings.deepseek_api_key:
                yield "data: [PROGRESS:⚠️ DeepSeek skipped — DEEPSEEK_API_KEY not set in Railway Variables]\n\n"
            else:
                _mw("DeepSeek", msg[:100])
                _ds_resp = ask_deepseek(msg, system=system)
                if _ds_resp and _ds_resp.startswith("["):
                    _me("DeepSeek", _ds_resp[:200])
                    yield f"data: [PROGRESS:⚠️ DeepSeek error: {_ds_resp[:120]}]\n\n"
                else:
                    _md("DeepSeek")
        except Exception as _ds_exc:
            try:
                _md("DeepSeek")
                _me("DeepSeek", str(_ds_exc)[:200])
            except Exception:
                pass
            yield f"data: [PROGRESS:⚠️ DeepSeek exception: {str(_ds_exc)[:100]}]\n\n"
        if _ds_resp and not _ds_resp.startswith("["):
            _store_mem(req.session_id, f"Q: {msg[:300]} A: {_ds_resp[:300]}")
            append_exchange(req.session_id, msg, _ds_resp)
            _il.record(msg, "DEEPSEEK", _ds_resp, "conversational", _cx, req.session_id)
            normalized = _ds_resp.replace("\n", " \n ")
            words = [w for w in normalized.split(" ") if w]
            buf = ""
            for word in words:
                buf += word + " "
                if len(buf) >= 60:
                    yield f"data: {buf.replace(chr(10), chr(92) + 'n')}\n\n"
                    buf = ""
            if buf.strip():
                yield f"data: {buf.replace(chr(10), chr(92) + 'n')}\n\n"
            yield f"data: [META:DEEPSEEK·conversational·{_mem_count}]\n\n"
            yield "data: [DONE]\n\n"
            return

        # ── 5. All tiers failed ───────────────────────────────────────────────
        yield "data: ⚠️ All response tiers unavailable (CLI, Gemini, Anthropic, DeepSeek). Please retry in a moment.\n\n"
        yield f"data: [META:UNAVAILABLE·conversational·{_mem_count}]\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@app.post("/chat/direct", response_model=ChatResponse, tags=["agent"])
@limiter.limit("30/minute")
def chat_direct(req: DirectChatRequest, request: Request):
    """Force a specific model — skips the classifier."""
    result = dispatch(req.message, force_model=req.model, session_id=req.session_id)
    if result["model_used"] is None:
        raise HTTPException(status_code=400, detail=result["response"])
    if not result["response"].startswith("["):
        append_exchange(req.session_id, req.message, result["response"])
    return _chat_response_from_result(result, req.session_id)


@app.get("/history/{session_id}", response_model=list[HistoryMessage], tags=["memory"])
def get_history(session_id: str):
    """Retrieve all messages for a session."""
    msgs = get_messages(session_id)
    return [HistoryMessage(role=m.type, content=m.content) for m in msgs]


@app.delete("/history/{session_id}", tags=["memory"])
def delete_history(session_id: str):
    """Clear all messages for a session."""
    clear_session(session_id)
    return {"ok": True, "session_id": session_id, "cleared": True}


@app.get("/collective-wisdom", tags=["meta"])
def collective_wisdom():
    """
    Collective intelligence state: per-model win rates by category,
    drift alerts, last Cloudinary sync timestamp, interaction count.
    """
    result = wisdom_store.wisdom_dict()
    result["drift_summary"] = wisdom_store.get_drift_summary()
    return result


class FeedbackRequest(BaseModel):
    session_id: str = "default"
    message: str                    # the original user message
    model_used: str                 # which model responded
    routed_by: str = ""             # how it was routed
    rating: int                     # 1 (wrong) to 5 (perfect)
    correction: str = ""            # optional: what the right answer was


@app.post("/feedback", tags=["learning"])
def submit_feedback(req: FeedbackRequest):
    """
    User feedback loop — closes the calibration cycle.

    When a user rates a response as wrong (rating <= 2), this endpoint:
    1. Records a loss outcome in wisdom_store for this model/category
    2. Saves the correction as an enriched memory (importance=4)
    3. Logs the incident so adapter._analyse() picks it up

    This makes the system actually learn from user corrections rather than
    just from whether responses START with "[" (the current error proxy).
    """
    try:
        from .learning.wisdom_store import wisdom_store as _ws
        from .memory.vector_memory import store_enriched_memory as _store_mem, ingest_external_memory

        model = req.model_used.upper()
        is_error = req.rating <= 2

        # Record outcome in wisdom store (penalises wrong answers)
        category = _ws._detect_category(req.routed_by, model)
        _ws.record_outcome(model, category, error=is_error)

        # High-importance memory: save the correction so future calls benefit
        if req.correction:
            ingest_external_memory(
                content=f"User correction for {model}: Original Q='{req.message[:200]}' "
                        f"was rated {req.rating}/5. Correct answer: {req.correction[:400]}",
                memory_type="preference" if req.rating <= 2 else "fact",
                importance=5 if req.rating == 1 else 4,
                source="user_feedback",
                session_id=req.session_id,
            )

        # Always store the rating as a fact regardless of whether correction was given
        ingest_external_memory(
            content=f"Response quality signal: {model} on '{req.message[:150]}' "
                    f"rated {req.rating}/5 (routed_by={req.routed_by})",
            memory_type="fact",
            importance=3,
            source="user_feedback",
            session_id=req.session_id,
        )

        return {
            "ok": True,
            "recorded": {
                "model": model,
                "category": category,
                "outcome": "loss" if is_error else "win",
                "correction_saved": bool(req.correction),
            }
        }
    except Exception as e:
        raise HTTPException(500, f"Feedback recording failed: {e}")


@app.get("/peer-review-stats", tags=["meta"])
def peer_review_stats():
    """
    Peer review impact analysis: compares error rates between peer-reviewed
    and non-reviewed high-complexity (>= 4) queries.
    """
    return adapter.analyse_peer_review_impact()


@app.post("/chat/ensemble", tags=["agent"])
@limiter.limit("10/minute")
def chat_ensemble(req: EnsembleRequest, request: Request):
    """
    Force ensemble mode: asks Claude, Gemini, and DeepSeek in parallel,
    then synthesizes with Haiku. Long responses (> 2000 chars) are
    uploaded to Cloudinary and a URL is returned.
    """
    from .learning.ensemble import ensemble_voter as _ev
    result = _ev.vote(req.message, complexity=5, session_id=req.session_id)
    response = result["response"] or "[Ensemble failed to produce a response]"
    if not response.startswith("["):
        append_exchange(req.session_id, req.message, response)
    return {
        "response": response,
        "model_used": "ENSEMBLE",
        "models_used": result["models_used"],
        "routed_by": "ensemble_forced",
        "session_id": req.session_id,
        "is_ensemble": True,
        "disagreement_detected": result["disagreement_detected"],
        "cloudinary_url": result["cloudinary_url"],
    }


@app.get("/wisdom/reload", tags=["meta"])
def wisdom_reload():
    """
    Hot-reload collective wisdom from Cloudinary without restarting.
    Useful after a container rebuild or when syncing across instances.
    """
    try:
        url = wisdom_store._pool.get("cloudinary_backup_url")
        if not url:
            return {"ok": False, "reason": "No Cloudinary backup URL stored yet — run 500+ interactions first."}
        fresh = wisdom_store._download_from_cloudinary(url)
        if fresh is None:
            return {"ok": False, "reason": "Download from Cloudinary failed — check credentials and URL."}
        with wisdom_store._lock:
            wisdom_store._pool.update(fresh)
        return {
            "ok": True,
            "synced_from": url,
            "last_synced_ts": wisdom_store._pool.get("last_synced_ts"),
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


class MobileBuildRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(default="A Flutter app built by Super Agent", max_length=200)
    platform: str = Field(default="android", description="android | ios")
    flutter_code: str = Field(default="", description="Optional custom lib/main.dart content")
    org_id: str = Field(default="com.superagent", max_length=64)


@app.post("/build/mobile", tags=["mobile"])
@limiter.limit("5/hour")
def build_mobile(req: MobileBuildRequest, request: Request):
    """
    Autonomous Flutter mobile app build pipeline.

    1. Scaffold Flutter project in /workspace
    2. Optionally inject custom lib/main.dart
    3. Build debug APK (Android) or trigger GitHub Actions (iOS)
    4. Upload APK to Cloudinary → return download URL
    5. Push project to GitHub → return repo URL
    """
    import json as _json
    from .tools.flutter_tools import (
        flutter_create_project,
        flutter_build_apk,
        upload_build_artifact,
        flutter_git_push,
    )

    platform = req.platform.lower()
    log_lines = []

    try:
        # Step 1: Create project
        create_out = flutter_create_project.invoke({
            "project_name": req.project_name,
            "org_id": req.org_id,
            "description": req.description,
        })
        log_lines.append(create_out)

        project_path = f"/workspace/{req.project_name}"

        # Step 2: Inject custom Dart code if provided
        if req.flutter_code.strip():
            from pathlib import Path as _Path
            main_dart = _Path(project_path) / "lib" / "main.dart"
            main_dart.write_text(req.flutter_code)
            log_lines.append(f"Injected custom lib/main.dart ({len(req.flutter_code)} chars)")

        apk_url = None
        repo_url = None

        if platform == "android":
            # Step 3a: Build APK
            build_out = flutter_build_apk.invoke({"project_path": project_path})
            log_lines.append(build_out)

            # Step 4: Upload to Cloudinary
            apk_path = f"{project_path}/build/app/outputs/flutter-apk/app-debug.apk"
            upload_out = upload_build_artifact.invoke({
                "file_path": apk_path,
                "filename": f"builds/{req.project_name}_android_{int(__import__('time').time())}",
            })
            try:
                upload_data = _json.loads(upload_out)
                apk_url = upload_data.get("url")
            except Exception:
                apk_url = upload_out
            log_lines.append(f"Cloudinary: {apk_url}")

        elif platform == "ios":
            log_lines.append(
                "iOS builds run on GitHub Actions (macos-latest runner). "
                "Push to mobile/ to trigger the workflow, or use workflow_dispatch. "
                "Download link will appear in the Actions run summary."
            )

        # Step 5: Push to GitHub
        push_out = flutter_git_push.invoke({
            "project_path": project_path,
            "repo_name": req.project_name,
            "commit_message": f"Initial {req.project_name} Flutter project",
        })
        log_lines.append(push_out)
        # Extract repo URL from first line
        repo_url = push_out.split("\n")[0].replace("Repo: ", "").strip()

        install_guide_url = str(request.base_url) + "install-guide"

        return {
            "project_name": req.project_name,
            "platform": platform,
            "apk_url": apk_url,
            "repo_url": repo_url,
            "install_guide_url": install_guide_url,
            "build_log": "\n---\n".join(log_lines),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Build failed: {e}\n\nLog:\n" + "\n".join(log_lines))


@app.get("/install-guide", tags=["mobile"], response_class=FileResponse)
def install_guide():
    """
    Return the mobile app installation guide (Markdown).
    Covers Android APK sideloading and iOS AltStore installation step-by-step.
    """
    guide_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "INSTALL_GUIDE.md")
    if os.path.isfile(guide_path):
        return FileResponse(guide_path, media_type="text/markdown", filename="INSTALL_GUIDE.md")
    return {"error": "Install guide not found"}


@app.get("/stats", tags=["meta"])
def stats():
    """
    Combined intelligence stats:
    - Cache hits/misses, estimated token savings and cost saved
    - Interaction log summary (total requests, model distribution, error rate)
    """
    return {
        "cache": cache.stats(),
        "interactions": insight_log.summary(),
    }


@app.get("/stats/report", tags=["meta"])
def stats_report():
    """
    Combined model usage + cost data for daily reports.
    Returns normalized model names compatible with cost_ledger naming.
    """
    return {
        "interactions": insight_log.normalized_summary(),
        "spend": _spend_summary(),
        "cache": cache.stats(),
    }


@app.get("/daily-review", tags=["meta"])
def daily_review():
    """
    Return the most recent nightly Claude Code review report.
    Generated at 23:00 UTC — advisory only, safe_to_auto_apply is always false.
    """
    from .learning.nightly_review import get_latest_review
    result = get_latest_review()
    if result is None:
        return {"message": "No nightly review available yet — first run at 23:00 UTC."}
    return result


@app.get("/daily-review/list", tags=["meta"])
def daily_review_list():
    """List all available nightly review dates (newest first)."""
    from .learning.nightly_review import list_review_dates
    return {"dates": list_review_dates()}


@app.get("/weekly-review", tags=["meta"])
def weekly_review():
    """
    Return the most recent weekly Opus 4.6 review report.
    Generated every Sunday at 23:00 UTC — strategic 7-day retrospective.
    """
    from .learning.weekly_review import get_latest_review as _get
    result = _get()
    if result is None:
        return {"message": "No weekly review available yet — first run this Sunday at 23:00 UTC."}
    return result


@app.get("/weekly-review/list", tags=["meta"])
def weekly_review_list():
    """List all available weekly review dates (newest first)."""
    from .learning.weekly_review import list_review_dates as _list
    return {"dates": _list()}


@app.post("/run-nightly-review", tags=["meta"])
def trigger_nightly_review():
    """
    Manually trigger the nightly review immediately (runs in background thread).
    Returns instantly — poll GET /daily-review for the result.
    Useful for testing the full review + vault note + CLAUDE.md refresh pipeline.
    """
    import threading as _t
    from .learning.nightly_review import run_nightly_review
    _t.Thread(target=run_nightly_review, daemon=True).start()
    return {"status": "started", "message": "Nightly review running in background — poll GET /daily-review for result."}


@app.post("/run-weekly-review", tags=["meta"])
def trigger_weekly_review():
    """
    Manually trigger the weekly review immediately (runs in background thread).
    Returns instantly — poll GET /weekly-review for the result.
    """
    import threading as _t
    from .learning.weekly_review import run_weekly_review
    _t.Thread(target=run_weekly_review, daemon=True).start()
    return {"status": "started", "message": "Weekly review running in background — poll GET /weekly-review for result."}


@app.get("/cycle-log", tags=["meta"])
def cycle_log_endpoint():
    """Cycle log: last 100 entries with summary statistics."""
    from .learning.improvement_cycle import get_cycle_log, get_cycle_summary
    entries = get_cycle_log()
    return {"total": len(entries), "summary": get_cycle_summary(), "entries": entries}


@app.get("/cycle-log/rejected", tags=["meta"])
def cycle_log_rejected():
    """Most recent rejected and NO_SAFE_IMPROVEMENT cycle entries (up to 20)."""
    from .learning.improvement_cycle import cycle_log as _cl
    entries = sorted(
        _cl.get_rejected(20) + _cl.get_no_safe(10),
        key=lambda x: x.get("recorded_at", ""),
        reverse=True,
    )[:20]
    return {"count": len(entries), "entries": entries}


@app.get("/cycle-log/summary", tags=["meta"])
def cycle_log_summary():
    """Aggregated cycle statistics: totals by decision and most recent cycle date."""
    from .learning.improvement_cycle import get_cycle_summary
    return get_cycle_summary()


@app.get("/pro-cache/stats", tags=["meta"])
def pro_cache_stats():
    """Pro CLI response cache statistics: hit rate, tokens saved, estimated cost saved."""
    from .cache.response_cache import cache
    return cache.stats()


@app.get("/pro-usage", tags=["meta"])
def pro_usage():
    """Pro CLI quota usage: daily/weekly stats, API call savings, and daily-limit ETA."""
    from .learning.pro_usage_tracker import get_daily_summary, get_weekly_summary, predict_daily_limit_eta
    from .learning.pro_router import get_status
    return {
        "pro_status": get_status(),
        "daily": get_daily_summary(),
        "weekly": get_weekly_summary(),
        "limit_eta": predict_daily_limit_eta(),
    }


@app.get("/n8n/test-results", tags=["n8n"])
def n8n_test_results():
    """Last 50 n8n workflow auto-test results with pass/fail counts."""
    from .tools.n8n_tester import get_test_results
    results = get_test_results(50)
    passed = sum(1 for r in results if r.get("passed") and not r.get("skipped"))
    failed = sum(1 for r in results if not r.get("passed") and not r.get("skipped"))
    return {"total": len(results), "passed": passed, "failed": failed, "results": results}


@app.post("/app-context/test", tags=["meta"])
def app_context_test(req: ChatRequest):
    """
    Parse and preview an [APP_CONTEXT] block without dispatching.
    Use from the mobile app to verify metadata is being read correctly.
    Returns the parsed fields, clean message, routing decision, and
    the exact Gemini prompt that would be built for location requests.
    """
    from .routing.app_context_parser import parse_app_context, is_location_request, build_location_prompt
    ctx, clean = parse_app_context(req.message)
    if not ctx:
        return {"parsed": False, "message": "No [APP_CONTEXT] block found in message"}
    is_loc = is_location_request(ctx)
    return {
        "parsed": True,
        "metadata": ctx,
        "clean_message": clean,
        "is_location_request": is_loc,
        "would_route_to": "GEMINI_CLI" if is_loc else "normal_pipeline",
        "location_prompt_preview": build_location_prompt(ctx) if is_loc else None,
    }


@app.get("/anomaly-alerts/recent", tags=["meta"])
def anomaly_alerts_recent():
    """Last 50 anomaly alerts (error spikes, budget, disk, n8n failures)."""
    from .learning.anomaly_alerter import get_recent_alerts
    return {"alerts": get_recent_alerts(50)}


@app.get("/session-profiles", tags=["meta"])
def session_profiles_endpoint():
    """Adaptive session routing profiles: per-session model preferences and routing hints."""
    from .learning.session_profile import session_profile
    profiles = session_profile.get_all()
    active = [p for p in profiles if p.get("routing_hint_active")]
    return {"total": len(profiles), "routing_hint_active": len(active), "profiles": profiles}


@app.get("/prompt-library", tags=["meta"])
def prompt_library_endpoint():
    """Self-improving prompt library: all tracked prompts with version counts and error rates."""
    from .learning.prompt_library import prompt_library
    return prompt_library.get_summary()


@app.get("/prompt-library/{name}", tags=["meta"])
def prompt_library_history(name: str):
    """Version history and error-rate metrics for a specific prompt."""
    from .learning.prompt_library import prompt_library, TRACKED_PROMPTS
    if name not in TRACKED_PROMPTS:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Unknown prompt '{name}'. Tracked: {TRACKED_PROMPTS}")
    return {"name": name, "versions": prompt_library.get_history(name)}


@app.get("/improvement-status", tags=["meta"])
def improvement_status():
    """
    Show all improvement monitors: what was applied, health check results,
    rollback branches, and final status (monitoring / stable / rolled_back).
    """
    from .learning.improvement_monitor import list_monitors
    monitors = list_monitors()
    return {
        "total": len(monitors),
        "monitoring": sum(1 for m in monitors if m.get("status") == "monitoring"),
        "stable": sum(1 for m in monitors if m.get("status") == "stable"),
        "rolled_back": sum(1 for m in monitors if m.get("status") == "rolled_back"),
        "monitors": monitors,
    }


@app.get("/dashboard/improvements", tags=["meta"])
def dashboard_improvements():
    """
    Aggregate improvement dashboard — combines daily review, weekly review,
    cycle log, improvement monitors, and anomaly alerts into a single view.
    """
    from datetime import datetime, timezone
    from .learning.nightly_review import get_latest_review as _daily
    from .learning.weekly_review import get_latest_review as _weekly
    from .learning.improvement_cycle import get_cycle_log, get_cycle_summary
    from .learning.improvement_monitor import list_monitors
    from .learning.anomaly_alerter import get_recent_alerts

    # Daily review
    daily = _daily()
    daily_section = {"available": False}
    if daily:
        daily_section = {
            "available": True,
            "date": daily.get("date", ""),
            "suggestions": daily.get("feature_improvements", daily.get("suggestions", [])),
            "health": daily.get("overall_health", ""),
            "auto_applied": daily.get("_auto_applied", []),
        }

    # Weekly review
    weekly = _weekly()
    weekly_section = {"available": False}
    if weekly:
        weekly_section = {
            "available": True,
            "week_ending": weekly.get("date", weekly.get("week_ending", "")),
            "suggestions": weekly.get("feature_improvements", weekly.get("suggestions", [])),
            "auto_applied": weekly.get("_auto_applied", []),
        }

    # Cycle log
    cycle_summary = get_cycle_summary()
    recent_cycles = get_cycle_log()[:20]  # last 20

    # Improvement monitors
    monitors = list_monitors()
    improvements = {
        "total": len(monitors),
        "monitoring": sum(1 for m in monitors if m.get("status") == "monitoring"),
        "stable": sum(1 for m in monitors if m.get("status") == "stable"),
        "rolled_back": sum(1 for m in monitors if m.get("status") == "rolled_back"),
        "monitors": monitors[-10:],  # last 10
    }

    # Recent alerts
    alerts = get_recent_alerts(10)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "daily_review": daily_section,
        "weekly_review": weekly_section,
        "cycle_summary": cycle_summary,
        "recent_cycles": recent_cycles,
        "applied_improvements": improvements,
        "recent_alerts": alerts,
    }


@app.get("/dashboard/stats-snapshot", tags=["meta"])
def stats_snapshot():
    """
    Single-call stats snapshot for the self-improvement agent.
    Aggregates: model rankings, routing decisions, health, spend,
    pro-usage, trends, cycle log, improvement monitors, peer-review,
    and anomaly alerts — all in one JSON blob.
    """
    from datetime import datetime, timezone
    import traceback as _tb

    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None

    # ── Rankings & routing ──────────────────────────────────────────
    wisdom_data   = _safe(lambda: wisdom_store.get_collective_wisdom())
    report_data   = _safe(lambda: adapter.get_stats_report())

    # ── Spend & pro usage ───────────────────────────────────────────
    spend_data    = _safe(lambda: _spend_summary())

    from .learning.pro_usage_tracker import get_daily_summary as _pu_daily, get_weekly_summary as _pu_weekly
    pro_data      = _safe(lambda: {"daily": _pu_daily(), "weekly": _pu_weekly()})

    # ── Trends ──────────────────────────────────────────────────────
    trends_data   = _safe(lambda: get_trends(hours=24))

    # ── Cycle log ───────────────────────────────────────────────────
    from .learning.improvement_cycle import get_cycle_summary, get_cycle_log
    cycle_data    = _safe(lambda: {
        "summary": get_cycle_summary(),
        "recent": get_cycle_log()[:10],
    })

    # ── Improvement monitors ────────────────────────────────────────
    from .learning.improvement_monitor import list_monitors
    monitors      = _safe(lambda: list_monitors()) or []
    deploys_data  = {
        "total": len(monitors),
        "monitoring": sum(1 for m in monitors if m.get("status") == "monitoring"),
        "stable":     sum(1 for m in monitors if m.get("status") == "stable"),
        "rolled_back":sum(1 for m in monitors if m.get("status") == "rolled_back"),
        "recent": monitors[-5:],
    }

    # ── Peer review ─────────────────────────────────────────────────
    peer_data     = _safe(lambda: adapter.analyse_peer_review_impact())

    # ── Anomalies ───────────────────────────────────────────────────
    from .learning.anomaly_alerter import get_recent_alerts
    anomalies     = _safe(lambda: get_recent_alerts(20)) or []

    # ── Agent health ────────────────────────────────────────────────
    from .learning.agent_status_tracker import get_all_statuses
    agent_health  = _safe(lambda: get_all_statuses())

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rankings":     wisdom_data,
        "routing":      report_data,
        "spend":        spend_data,
        "pro_usage":    pro_data,
        "trends_24h":   trends_data,
        "cycle":        cycle_data,
        "deployments":  deploys_data,
        "peer_review":  peer_data,
        "anomalies":    anomalies,
        "agent_health": agent_health,
    }


@app.get("/wisdom", tags=["meta"])
def wisdom():
    """
    Adaptive self-improvement state:
    - Learned context injected into prompts
    - Haiku ceiling (max complexity Haiku handles)
    - Analysis history and routing notes
    """
    return adapter.wisdom_dict()


@app.get("/algorithms", tags=["algorithms"])
def list_algorithms():
    """
    List all self-built algorithms currently loaded in the algorithm store.
    Shows name, load status, and last refresh timestamp.
    """
    from .learning.algorithm_store import algorithm_store as _store
    return {
        "store": _store.status(),
        "algorithms": _store.list_algorithms(),
    }


@app.post("/algorithms/build", tags=["algorithms"])
@limiter.limit("2/hour")
def build_algorithms(request: Request):
    """
    Manually trigger a build of self-generated algorithms from the current
    wisdom store and insight log data.
    New algorithms are committed to the 'super-agent-algorithms' GitHub repo.
    Runs automatically every 200 interactions.
    """
    from .learning.algorithm_builder import build_and_commit_algorithms as _build
    try:
        summary = _build()
        return {"ok": True, **summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Algorithm build failed: {e}")


@app.get("/algorithms/reload", tags=["algorithms"])
def reload_algorithms():
    """
    Hot-reload algorithms from the GitHub repo without restarting.
    Useful after a manual commit or forced build.
    """
    from .learning.algorithm_store import algorithm_store as _store
    try:
        _store._refresh()
        return {"ok": True, "store": _store.status()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reload failed: {e}")


@app.get("/build/stream", tags=["build"])
async def build_stream():
    """
    SSE stream of the current Flutter build progress.
    Tails /workspace/build_progress.log in real-time — subscribe while a build is running
    to see exactly what step is executing instead of a blank 'Thinking...' spinner.
    Closes automatically when the build finishes (detects '🏁 Build pipeline complete').
    Uses async sleep — does NOT block the event loop.
    """
    from .tools.flutter_tools import BUILD_PROGRESS_LOG
    import asyncio
    import time as _time

    _SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    async def _tail():
        pos = 0
        deadline = _time.time() + 900  # max 15 min stream
        yield "data: [BUILD_STREAM_START]\n\n"
        while _time.time() < deadline:
            try:
                if BUILD_PROGRESS_LOG.exists():
                    text = BUILD_PROGRESS_LOG.read_text(encoding="utf-8")
                    if len(text) > pos:
                        new_lines = text[pos:]
                        pos = len(text)
                        for line in new_lines.splitlines():
                            if line.strip():
                                # Escape any newlines inside a single log line
                                yield f"data: {line.strip().replace(chr(10), ' ')}\n\n"
                        if "Build pipeline complete" in new_lines or "FAILED" in new_lines:
                            yield "data: [BUILD_STREAM_END]\n\n"
                            return
            except Exception:
                pass
            await asyncio.sleep(1)   # non-blocking — other requests still served
        yield "data: [BUILD_STREAM_TIMEOUT]\n\n"

    return StreamingResponse(_tail(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/activity/stream", tags=["activity"])
async def activity_stream():
    """
    SSE stream of all background autonomous activity.

    Subscribe to this endpoint to watch what Super Agent is doing in the background:
    health checks, post-deploy validation, Railway webhook responses, n8n monitoring,
    nightly review, weekly review, and any autonomous self-improvement actions.

    Works exactly like /build/stream but for all background operations, not just builds.
    Stream stays open; closes after 30 minutes of inactivity.
    """
    import asyncio
    import time as _time

    _SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    async def _tail():
        pos = 0
        last_activity = _time.time()
        deadline = _time.time() + 1800  # 30 min max
        yield "data: [ACTIVITY_STREAM_START]\n\n"
        while _time.time() < deadline:
            try:
                if ACTIVITY_LOG.exists():
                    text = ACTIVITY_LOG.read_text(encoding="utf-8")
                    if len(text) > pos:
                        new_lines = text[pos:]
                        pos = len(text)
                        last_activity = _time.time()
                        for line in new_lines.splitlines():
                            if line.strip():
                                yield f"data: {line.strip().replace(chr(10), ' ')}\n\n"
            except Exception:
                pass
            await asyncio.sleep(2)
        yield "data: [ACTIVITY_STREAM_TIMEOUT]\n\n"

    return StreamingResponse(_tail(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/activity/recent", tags=["activity"])
def activity_recent(lines: int = 50):
    """
    Return the most recent background activity log entries as JSON.
    Useful for polling — shows the last N lines (default 50, max 200).
    """
    n = min(max(lines, 1), 200)
    entries = _activity_recent_lines(n)
    return {
        "lines": entries,
        "total_returned": len(entries),
        "log_path": str(ACTIVITY_LOG),
    }


@app.post("/activity/log", tags=["activity"])
def activity_log_external(payload: dict):
    """
    Write an external log entry to the Super Agent activity log.
    Used by n8n workflows to report health checks, anomalies, and agent status
    so Super Agent can see patterns across time and anticipate issues.

    Expected payload: {"source": "n8n_health_monitor", "message": "..."}
    """
    source = str(payload.get("source", "external"))[:40]
    message = str(payload.get("message", ""))[:1000]
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    bg_log(message, source=source)
    return {"ok": True, "logged": message[:80]}


@app.get("/downloads/status", tags=["build"])
def download_status():
    """List all registered APK download links and whether they are still valid."""
    from .tools.flutter_tools import get_apk_download_status
    return get_apk_download_status()


@app.get("/downloads/{token}/{filename}", tags=["build"])
async def download_artifact(token: str, filename: str):
    """
    Serve a build artifact (APK) directly from the Railway container filesystem.

    URL structure: /downloads/{token}/{filename.apk}
    - token: a random 22-char URL-safe string generated at upload time.
              It acts as the access credential — no X-Token header needed
              (so it works from a mobile Chrome browser).
    - filename: the original APK filename (e.g. app-debug.apk).

    The file is stored at /workspace/apk_downloads/{token}/{filename}.
    Links are temporary: they survive container restarts only if the volume persists.
    """
    from pathlib import Path as _Path
    import re

    # Sanitise inputs — prevent path traversal
    if not re.match(r'^[A-Za-z0-9_\-]{10,64}$', token):
        raise HTTPException(status_code=400, detail="Invalid token")
    if not re.match(r'^[A-Za-z0-9_\-\.]{1,64}$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = _Path("/workspace/apk_downloads") / token / filename
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "File not found. Railway container may have been redeployed since this link was created. "
                "Ask Super Agent to retry the upload or rebuild the APK."
            ),
        )

    return FileResponse(
        path=str(file_path),
        media_type="application/vnd.android.package-archive",
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/build/status", tags=["build"])
def build_status():
    """
    Return the current build progress log as JSON.
    Useful for polling — returns last 50 lines of /workspace/build_progress.log.
    """
    from .tools.flutter_tools import BUILD_PROGRESS_LOG
    try:
        if not BUILD_PROGRESS_LOG.exists():
            return {"running": False, "lines": [], "last": "No build started yet"}
        lines = BUILD_PROGRESS_LOG.read_text(encoding="utf-8").splitlines()
        last_50 = lines[-50:] if len(lines) > 50 else lines
        last = lines[-1] if lines else ""
        running = bool(lines) and "Build pipeline complete" not in last and "FAILED" not in last
        return {"running": running, "lines": last_50, "last": last, "total_lines": len(lines)}
    except Exception as e:
        return {"running": False, "lines": [], "last": f"Error: {e}"}


@app.get("/n8n/connection-info", tags=["n8n"])
async def n8n_connection_info(request: Request):
    """
    Returns the pre-filled HTTP Request node configuration for calling Super Agent from n8n.
    Use this instead of the Anthropic credential node — Super Agent already has Claude
    configured, handles routing/memory, and costs nothing extra per call.

    Public endpoint — no X-Token required (contains no secrets, just field names).
    """
    domain = settings.railway_public_domain or str(request.base_url).rstrip("/")
    base = f"https://{domain}" if not domain.startswith("http") else domain.rstrip("/")
    return {
        "description": "Use an HTTP Request node in n8n with these settings to call Super Agent as your AI.",
        "method": "POST",
        "url": f"{base}/chat",
        "headers": {
            "Content-Type": "application/json",
            "X-Token": "<<your UI_PASSWORD from Railway Variables>>",
        },
        "body_example": {
            "message": "{{ $json.input }}",
            "session_id": "n8n-{{ $workflow.id }}",
        },
        "response_field": "response",
        "all_response_fields": ["response", "model_used", "routed_by", "session_id", "confidence_score"],
        "tips": [
            "Map {{ $json.response }} to get the AI reply text.",
            "Use a unique session_id per workflow to keep conversation history separate.",
            "The X-Token value is your UI_PASSWORD Railway variable — check Railway → Variables.",
            "Leave X-Token header out entirely if UI_PASSWORD is not set on your deployment.",
        ],
        "streaming_url": f"{base}/chat/stream",
    }


# ── Direct n8n Workflow Management API ─────────────────────────────────────────
# Zero-cost REST proxy to n8n — no AI models called, no Anthropic credits used.
# Create, update, activate, delete workflows directly via super-agent URL.


class WorkflowCreateRequest(BaseModel):
    """Create a workflow from a full JSON definition or name + nodes."""
    name: str = Field(..., description="Workflow display name")
    nodes: list = Field(default=[], description="Array of node objects")
    connections: dict = Field(default={}, description="Node connections map")
    workflow_json: dict | None = Field(default=None, description="Full workflow JSON (overrides nodes/connections)")
    activate: bool = Field(default=False, description="Activate immediately after creation")


class WorkflowUpdateRequest(BaseModel):
    """Update an existing workflow."""
    nodes: list | None = Field(default=None, description="Updated node array")
    connections: dict | None = Field(default=None, description="Updated connections")
    workflow_json: dict | None = Field(default=None, description="Full workflow JSON")
    name: str | None = Field(default=None, description="New name")
    activate: bool | None = Field(default=None, description="Set active state")


@app.get("/n8n/workflows", tags=["n8n"])
async def n8n_list_all_workflows(request: Request):
    """List all workflows in n8n with their active status. No AI credits used."""
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured (N8N_BASE_URL/N8N_API_KEY missing)")

    base = settings.n8n_base_url.rstrip("/")
    headers = {"X-N8N-API-KEY": settings.n8n_api_key, "Accept": "application/json"}
    all_workflows = []
    cursor = None

    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(20):
            url = f"{base}/api/v1/workflows?limit=100"
            if cursor:
                url += f"&cursor={cursor}"
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            for wf in data.get("data", []):
                all_workflows.append({
                    "id": wf["id"],
                    "name": wf["name"],
                    "active": wf.get("active", False),
                    "createdAt": wf.get("createdAt"),
                    "updatedAt": wf.get("updatedAt"),
                })
            cursor = data.get("nextCursor")
            if not cursor:
                break

    return {"count": len(all_workflows), "workflows": all_workflows}


@app.get("/n8n/workflows/{workflow_id}", tags=["n8n"])
async def n8n_get_workflow_detail(workflow_id: str, request: Request):
    """Get full workflow JSON by ID. No AI credits used."""
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured")

    base = settings.n8n_base_url.rstrip("/")
    headers = {"X-N8N-API-KEY": settings.n8n_api_key, "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{base}/api/v1/workflows/{workflow_id}", headers=headers)
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
        resp.raise_for_status()
        return resp.json()


@app.post("/n8n/workflows", tags=["n8n"])
async def n8n_create_new_workflow(body: WorkflowCreateRequest, request: Request):
    """
    Create a new workflow in n8n directly. No AI credits used.
    Accepts either workflow_json (full definition) or name + nodes + connections.
    Set activate=true to make it live immediately.
    """
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured")

    base = settings.n8n_base_url.rstrip("/")
    headers = {
        "X-N8N-API-KEY": settings.n8n_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if body.workflow_json:
        payload = body.workflow_json.copy()
        payload.pop("id", None)
        payload.pop("versionId", None)
        if body.name:
            payload["name"] = body.name
    else:
        payload = {
            "name": body.name,
            "nodes": body.nodes,
            "connections": body.connections,
            "settings": {"executionOrder": "v1"},
        }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{base}/api/v1/workflows", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        created = resp.json()
        wf_id = created.get("id")

        if body.activate and wf_id:
            act_resp = await client.patch(
                f"{base}/api/v1/workflows/{wf_id}",
                json={"active": True},
                headers=headers,
            )
            if act_resp.status_code < 400:
                created["active"] = True

    bg_log(f"Workflow created via API: {created.get('name')} (ID: {wf_id}, active: {created.get('active')})", source="n8n_api")
    return {
        "success": True,
        "id": wf_id,
        "name": created.get("name"),
        "active": created.get("active", False),
        "message": f"Workflow '{created.get('name')}' created" + (" and activated" if body.activate else ""),
    }


@app.put("/n8n/workflows/{workflow_id}", tags=["n8n"])
async def n8n_update_existing_workflow(workflow_id: str, body: WorkflowUpdateRequest, request: Request):
    """Update an existing workflow. No AI credits used."""
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured")

    base = settings.n8n_base_url.rstrip("/")
    headers = {
        "X-N8N-API-KEY": settings.n8n_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        current = await client.get(f"{base}/api/v1/workflows/{workflow_id}", headers=headers)
        if current.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
        current.raise_for_status()
        wf = current.json()

        if body.workflow_json:
            payload = body.workflow_json.copy()
            payload.pop("id", None)
        else:
            payload = {}
            if body.nodes is not None:
                payload["nodes"] = body.nodes
            if body.connections is not None:
                payload["connections"] = body.connections
            if body.name is not None:
                payload["name"] = body.name

        if payload:
            resp = await client.put(f"{base}/api/v1/workflows/{workflow_id}", json=payload, headers=headers)
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            wf = resp.json()

        if body.activate is not None:
            act_resp = await client.patch(
                f"{base}/api/v1/workflows/{workflow_id}",
                json={"active": body.activate},
                headers=headers,
            )
            if act_resp.status_code < 400:
                wf["active"] = body.activate

    bg_log(f"Workflow updated via API: {wf.get('name')} (ID: {workflow_id})", source="n8n_api")
    return {"success": True, "id": workflow_id, "name": wf.get("name"), "active": wf.get("active", False)}


@app.patch("/n8n/workflows/{workflow_id}/activate", tags=["n8n"])
async def n8n_activate(workflow_id: str, request: Request):
    """Activate a workflow. No AI credits used."""
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured")

    base = settings.n8n_base_url.rstrip("/")
    headers = {"X-N8N-API-KEY": settings.n8n_api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(f"{base}/api/v1/workflows/{workflow_id}", json={"active": True}, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return {"success": True, "id": workflow_id, "active": True}


@app.patch("/n8n/workflows/{workflow_id}/deactivate", tags=["n8n"])
async def n8n_deactivate(workflow_id: str, request: Request):
    """Deactivate a workflow. No AI credits used."""
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured")

    base = settings.n8n_base_url.rstrip("/")
    headers = {"X-N8N-API-KEY": settings.n8n_api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(f"{base}/api/v1/workflows/{workflow_id}", json={"active": False}, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return {"success": True, "id": workflow_id, "active": False}


@app.delete("/n8n/workflows/{workflow_id}", tags=["n8n"])
async def n8n_delete_existing_workflow(workflow_id: str, request: Request):
    """Delete a workflow permanently. No AI credits used."""
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured")

    base = settings.n8n_base_url.rstrip("/")
    headers = {"X-N8N-API-KEY": settings.n8n_api_key}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(f"{base}/api/v1/workflows/{workflow_id}", headers=headers)
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

    bg_log(f"Workflow deleted via API: {workflow_id}", source="n8n_api")
    return {"success": True, "id": workflow_id, "deleted": True}


@app.post("/n8n/workflows/{workflow_id}/execute", tags=["n8n"])
async def n8n_execute_existing_workflow(workflow_id: str, request: Request):
    """Manually trigger a workflow execution. No AI credits used."""
    import httpx
    from .config import settings
    if not settings.n8n_base_url or not settings.n8n_api_key:
        raise HTTPException(status_code=503, detail="n8n not configured")

    base = settings.n8n_base_url.rstrip("/")
    headers = {"X-N8N-API-KEY": settings.n8n_api_key, "Content-Type": "application/json"}

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{base}/api/v1/workflows/{workflow_id}/run", json=body, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()


@app.post("/webhook/railway", tags=["webhook"])
@limiter.limit("20/minute")
async def railway_webhook(request: Request):
    """
    Receives Railway deployment event webhooks.
    Set this URL in Railway: Project Settings → Webhooks → https://your-domain/webhook/railway

    On DEPLOY_SUCCESS: runs post-deploy health check + regenerates stale APK links.
    On DEPLOY_FAILED / CRASHED: immediately triggers self_improve_agent to
    read Railway logs, diagnose the failure, and attempt autonomous repair.

    No auth header needed — Railway sends events server-side. We validate by
    checking the payload structure rather than a secret (Railway doesn't sign payloads).
    """
    import threading as _threading

    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "detail": "Invalid JSON payload"}

    status = (
        payload.get("status")
        or payload.get("type")
        or payload.get("deploymentStatus")
        or ""
    ).upper()

    service = payload.get("service", {}).get("name", "unknown") if isinstance(payload.get("service"), dict) else str(payload.get("service", "unknown"))

    def _handle():
        try:
            from .agents.self_improve_agent import run_self_improve_agent
            bg_log(f"Railway webhook received: {status} for service '{service}'", source="railway_webhook")
            if status in ("SUCCESS", "DEPLOY_SUCCESS", "COMPLETE"):
                run_self_improve_agent(
                    f"Railway deploy SUCCEEDED for service '{service}'. Do:\n"
                    "1. Verify the app is responding: run_shell_command('curl -s -o /dev/null -w \"%{http_code}\" http://127.0.0.1:8000/health')\n"
                    "2. If /workspace/apk_downloads exists, regenerate download links (old tokens are invalid after redeploy)\n"
                    "3. Check db_health_check() — DB still reachable after redeploy?\n"
                    "4. Report OK or flag any issue.",
                    authorized=False,
                )
                bg_log(f"Railway webhook: post-deploy health check complete for '{service}'", source="railway_webhook")
                # Diff-aware instant review — review only what just changed
                # Skipped when credit tier is reduced/minimal/critical (expensive per deploy)
                try:
                    if not _should_run("diff_review"):
                        bg_log("Diff review skipped — credit throttle active", source="railway_webhook")
                    else:
                        from .agents.self_improve_agent import run_self_improve_agent as _sia
                        _sia(
                            "DIFF-AWARE POST-DEPLOY REVIEW — a new commit was just deployed. Do:\n"
                            "1. github_get_recent_commits(limit=1) — get the latest commit SHA and message\n"
                            "2. github_get_diff(sha) — read the diff of that commit\n"
                            "3. Review ONLY the changed lines: does this diff introduce a regression, "
                            "a broken import, an unhandled exception path, or a logic error?\n"
                            "4. If yes: describe the exact issue and the file+line. Do NOT fix — just report.\n"
                            "5. If clean: write one line to activity log confirming the diff looks safe.\n"
                            "Be fast — this runs on every deploy. Max 3 tool calls.",
                            authorized=False,
                        )
                        _ledger_record("CLAUDE", 1500, 600, category="code_review")
                except Exception:
                    pass
                # Seed deploy event into agent memory so future queries can reference it
                try:
                    from .memory.vector_memory import store_enriched_memory as _sem
                    _commit_msg = payload.get("commitMessage") or payload.get("deployment", {}).get("meta", {}).get("commitMessage", "unknown")
                    _commit_hash = payload.get("commitHash") or payload.get("deployment", {}).get("meta", {}).get("commitHash", "")
                    _sem(
                        "system",
                        f"Railway deploy SUCCEEDED for service '{service}'. "
                        f"Commit: {_commit_msg}. Hash: {_commit_hash[:8] if _commit_hash else 'unknown'}.",
                        memory_type="deploy_event",
                        importance=6,
                    )
                except Exception:
                    pass
                # Cost for success webhook handler
                _ledger_record("CLAUDE", 1500, 500, category="health_check")
            elif status in ("FAILED", "DEPLOY_FAILED", "CRASHED", "ERROR", "REMOVED"):
                bg_log(f"Railway webhook: FAILURE detected for '{service}' — launching autonomous investigation", source="railway_webhook")
                run_self_improve_agent(
                    f"Railway deploy FAILED/CRASHED for service '{service}'. Investigate NOW:\n"
                    "1. railway_get_logs() — what caused the failure?\n"
                    "2. railway_get_deployment_status() — current state\n"
                    "3. Identify the root cause from the logs\n"
                    "4. If it's a known fixable error (import error, missing env var, syntax error): "
                    "read the relevant file from GitHub, apply the fix, push, and trigger redeploy\n"
                    "5. Report exactly what failed and what you did.\n"
                    "Do NOT wait for user input — investigate and fix autonomously.",
                    authorized=False,
                )
                bg_log(f"Railway webhook: autonomous investigation complete for '{service}'", source="railway_webhook")
                # Seed failure event into agent memory (higher importance — failures are critical)
                try:
                    from .memory.vector_memory import store_enriched_memory as _sem
                    _commit_msg = payload.get("commitMessage") or payload.get("deployment", {}).get("meta", {}).get("commitMessage", "unknown")
                    _sem(
                        "system",
                        f"Railway deploy FAILED/CRASHED for service '{service}'. "
                        f"Status: {status}. Commit: {_commit_msg}. Autonomous investigation launched.",
                        memory_type="deploy_event",
                        importance=8,
                    )
                except Exception:
                    pass
                _ledger_record("CLAUDE", 2000, 1000, category="auto_fix")
        except Exception as _e:
            bg_log(f"Railway webhook handler error: {_e}", source="railway_webhook")

    _threading.Thread(target=_handle, daemon=True).start()
    return {"ok": True, "status_received": status, "service": service}


@app.get("/admin/live-log", tags=["admin"])
def admin_live_log(lines: int = 100):
    """
    Return recent activity across all Super Agent logs for external monitoring.
    Shows: build progress, insight log summary, recent errors, Railway status.
    Use this endpoint to monitor what Super Agent is doing from outside the container.
    """
    import json as _json
    from .tools.flutter_tools import BUILD_PROGRESS_LOG
    from .learning.insight_log import LOG_PATH as _INSIGHT_LOG

    report: dict = {}

    # Build progress
    try:
        if BUILD_PROGRESS_LOG.exists():
            blines = BUILD_PROGRESS_LOG.read_text(encoding="utf-8").splitlines()
            report["build_progress"] = blines[-lines:] if len(blines) > lines else blines
        else:
            report["build_progress"] = []
    except Exception as e:
        report["build_progress"] = [f"Error reading: {e}"]

    # Recent insight log entries (last 20 interactions)
    try:
        if os.path.exists(_INSIGHT_LOG):
            with open(_INSIGHT_LOG) as f:
                entries = _json.load(f)
            recent = entries[-20:] if len(entries) > 20 else entries
            report["recent_interactions"] = [
                {"model": e.get("model"), "route": e.get("routed_by"),
                 "error": e.get("error", False), "ts": e.get("timestamp", "")}
                for e in recent
            ]
            report["total_interactions"] = len(entries)
            errors = sum(1 for e in entries if e.get("error"))
            report["error_rate_pct"] = round(errors / len(entries) * 100, 1) if entries else 0
        else:
            report["recent_interactions"] = []
    except Exception as e:
        report["recent_interactions"] = [f"Error: {e}"]

    # Memory store size
    try:
        mem_log = os.path.join("/workspace", "agent_memories.jsonl")
        if os.path.exists(mem_log):
            with open(mem_log) as f:
                count = sum(1 for _ in f)
            report["memory_records"] = count
    except Exception:
        report["memory_records"] = "unknown"

    return report


@app.get("/storage/status", tags=["storage"])
def storage_status():
    """Show current Cloudinary storage usage and quota."""
    try:
        return get_storage_status()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Storage unavailable: {e}")


@app.post("/storage/upload", tags=["storage"])
def storage_upload(req: UploadRequest):
    """
    Upload a file to Cloudinary.
    Automatically deletes oldest assets if storage exceeds 1 GB.
    """
    try:
        return upload_file(req.file_path, req.resource_type, req.public_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


# ── Bridge website form endpoints ────────────────────────────────────────────

def _get_db_conn():
    """Return a psycopg2 connection using DATABASE_URL, or None if not configured."""
    import psycopg2
    raw = settings.database_url
    if not raw:
        return None
    url = raw.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def _ensure_bridge_tables():
    """Create bridge_leads and bridge_newsletter tables if they don't exist."""
    conn = _get_db_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bridge_leads (
                        id          SERIAL PRIMARY KEY,
                        first_name  TEXT,
                        last_name   TEXT,
                        email       TEXT,
                        phone       TEXT,
                        company     TEXT,
                        service     TEXT,
                        message     TEXT,
                        language    TEXT DEFAULT 'en',
                        timestamp   TEXT,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bridge_newsletter (
                        id          SERIAL PRIMARY KEY,
                        email       TEXT UNIQUE,
                        timestamp   TEXT,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
    finally:
        conn.close()


def _send_email(subject: str, body: str) -> None:
    """Send a plain-text notification email via Gmail SMTP.

    Requires Railway env vars SMTP_USER and SMTP_PASSWORD (Gmail App Password).
    Silently skips if credentials are not configured.
    """
    import smtplib
    from email.mime.text import MIMEText

    sender = settings.smtp_user
    password = settings.smtp_password
    recipient = settings.notify_email

    if not sender or not password:
        return  # SMTP not configured yet — skip silently

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"Bridge Website <{sender}>"
    msg["To"] = recipient

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
    except Exception:
        pass  # never let email failure break the form response


# Run once at startup
try:
    _ensure_bridge_tables()
except Exception:
    pass  # DB not yet available — Railway provisions it async


class LeadRequest(BaseModel):
    firstName: str = ""
    lastName: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    service: str = ""
    message: str = ""
    language: str = "en"
    timestamp: str = ""


class NewsletterRequest(BaseModel):
    email: str
    timestamp: str = ""


@app.post("/leads", tags=["website"])
@limiter.limit("10/hour")
def submit_lead(req: LeadRequest, request: Request):
    """Store a contact form submission and email bridge.digital.solution@gmail.com."""
    import json as _json
    from pathlib import Path as _Path

    # Primary: PostgreSQL
    conn = _get_db_conn()
    storage = "file"
    if conn:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO bridge_leads
                           (first_name, last_name, email, phone, company,
                            service, message, language, timestamp)
                           VALUES (%(firstName)s, %(lastName)s, %(email)s, %(phone)s,
                                   %(company)s, %(service)s, %(message)s,
                                   %(language)s, %(timestamp)s)""",
                        req.model_dump(),
                    )
            storage = "postgres"
        finally:
            conn.close()
    else:
        try:
            with _Path("/workspace/bridge_leads.jsonl").open("a") as f:
                f.write(_json.dumps(req.model_dump()) + "\n")
        except Exception:
            pass

    # Email notification
    full_name = f"{req.firstName} {req.lastName}".strip()
    _send_email(
        subject=f"[Bridge] New enquiry from {full_name or req.email}",
        body=(
            f"New contact form submission on Bridge website\n"
            f"{'=' * 48}\n\n"
            f"Name:     {full_name}\n"
            f"Email:    {req.email}\n"
            f"Phone:    {req.phone or '—'}\n"
            f"Company:  {req.company or '—'}\n"
            f"Service:  {req.service or '—'}\n"
            f"Language: {req.language}\n"
            f"Time:     {req.timestamp}\n\n"
            f"Message\n"
            f"-------\n"
            f"{req.message or '(no message)'}\n"
        ),
    )

    return {"ok": True, "storage": storage}


@app.get("/leads", tags=["website"])
def list_leads():
    """Return all Bridge contact form submissions (PostgreSQL only)."""
    conn = _get_db_conn()
    if conn is None:
        return {"ok": False, "error": "DATABASE_URL not configured"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, first_name, last_name, email, phone, company, "
                "service, message, language, timestamp, created_at "
                "FROM bridge_leads ORDER BY created_at DESC LIMIT 500"
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"ok": True, "leads": rows}
    finally:
        conn.close()


class ContactRequest(BaseModel):
    name: str = ""
    email: str = ""
    company: str = ""
    message: str = ""
    timestamp: str = ""


@app.post("/contact", tags=["website"])
@limiter.limit("10/hour")
def submit_contact(req: ContactRequest, request: Request):
    """Store a contact submission from the lovable website and email bridge.digital.solution@gmail.com."""
    import json as _json
    from pathlib import Path as _Path

    # Ensure table exists
    conn = _get_db_conn()
    storage = "file"
    if conn:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS bridge_contacts (
                            id         SERIAL PRIMARY KEY,
                            name       TEXT,
                            email      TEXT,
                            company    TEXT,
                            message    TEXT,
                            timestamp  TEXT,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)
                    cur.execute(
                        """INSERT INTO bridge_contacts (name, email, company, message, timestamp)
                           VALUES (%(name)s, %(email)s, %(company)s, %(message)s, %(timestamp)s)""",
                        req.model_dump(),
                    )
            storage = "postgres"
        finally:
            conn.close()
    else:
        try:
            with _Path("/workspace/bridge_contacts.jsonl").open("a") as f:
                f.write(_json.dumps(req.model_dump()) + "\n")
        except Exception:
            pass

    _send_email(
        subject=f"[Bridge] New enquiry from {req.name or req.email}",
        body=(
            f"New contact form submission on Bridge website (v2)\n"
            f"{'=' * 48}\n\n"
            f"Name:    {req.name}\n"
            f"Email:   {req.email}\n"
            f"Company: {req.company or '—'}\n"
            f"Time:    {req.timestamp}\n\n"
            f"Message\n"
            f"-------\n"
            f"{req.message or '(no message)'}\n"
        ),
    )

    # Fire n8n contact-alert webhook (non-blocking — WhatsApp + extra channels)
    _webhook_url = settings.n8n_contact_webhook_url
    if _webhook_url:
        import threading as _threading
        import requests as _req_mod
        def _fire_n8n():
            try:
                _req_mod.post(_webhook_url, json={
                    "name": req.name,
                    "email": req.email,
                    "company": req.company or "",
                    "message": req.message,
                    "timestamp": req.timestamp or "",
                }, timeout=5)
            except Exception:
                pass
        _threading.Thread(target=_fire_n8n, daemon=True).start()

    return {"ok": True, "storage": storage}


@app.post("/newsletter", tags=["website"])
@limiter.limit("10/hour")
def submit_newsletter(req: NewsletterRequest, request: Request):
    """Store a newsletter signup and email bridge.digital.solution@gmail.com."""
    import json as _json
    from pathlib import Path as _Path

    storage = "file"
    conn = _get_db_conn()
    if conn:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO bridge_newsletter (email, timestamp)
                           VALUES (%(email)s, %(timestamp)s)
                           ON CONFLICT (email) DO NOTHING""",
                        req.model_dump(),
                    )
            storage = "postgres"
        finally:
            conn.close()
    else:
        try:
            with _Path("/workspace/bridge_newsletter.jsonl").open("a") as f:
                f.write(_json.dumps(req.model_dump()) + "\n")
        except Exception:
            pass

    # Email notification
    _send_email(
        subject=f"[Bridge] New newsletter signup — {req.email}",
        body=(
            f"New newsletter subscription on Bridge website\n"
            f"{'=' * 48}\n\n"
            f"Email: {req.email}\n"
            f"Time:  {req.timestamp}\n"
        ),
    )

    return {"ok": True, "storage": storage}


@app.get("/newsletter", tags=["website"])
def list_newsletter():
    """Return all Bridge newsletter signups (PostgreSQL only)."""
    conn = _get_db_conn()
    if conn is None:
        return {"ok": False, "error": "DATABASE_URL not configured"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, timestamp, created_at "
                "FROM bridge_newsletter ORDER BY created_at DESC LIMIT 500"
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"ok": True, "signups": rows}
    finally:
        conn.close()


# ── Status / Metrics / Benchmark / Cost / Recipes ────────────────────────────

@app.get("/status/now", tags=["meta"])
def status_now():
    """
    Live single-call dashboard — everything happening right now in one response.
    Shows: active build progress, last 5 activity log lines, active task,
    current Railway deployment, n8n status, cost today, trend alerts.
    Poll every 10 seconds from the UI for a live status bar.
    """
    import time as _t
    report: dict = {}

    # Active build
    try:
        from .tools.flutter_tools import BUILD_PROGRESS_LOG
        if BUILD_PROGRESS_LOG.exists():
            blines = BUILD_PROGRESS_LOG.read_text(encoding="utf-8").splitlines()
            last3 = [l for l in blines[-3:] if l.strip()]
            last = blines[-1] if blines else ""
            report["build"] = {
                "running": bool(blines) and "complete" not in last.lower() and "FAILED" not in last,
                "last_lines": last3,
            }
        else:
            report["build"] = {"running": False, "last_lines": []}
    except Exception:
        report["build"] = {"running": False, "last_lines": []}

    # Last 5 activity log lines
    report["recent_activity"] = _activity_recent_lines(5)

    # Active task tracker
    try:
        from .routing.dispatcher import _read_active_task
        active, sid, task = _read_active_task()
        report["active_task"] = {"running": active, "session": sid, "task": task[:200] if task else ""}
    except Exception:
        report["active_task"] = {"running": False}

    # Trend alerts (from last snapshot — no live API call)
    try:
        trends = get_trends(hours=6)
        report["trend_alerts"] = trends.get("alerts", [])
        report["metrics_snapshot"] = {k: v["current"] for k, v in trends.get("metrics", {}).items()}
    except Exception:
        report["trend_alerts"] = []

    # Cost today
    try:
        spend = _spend_summary()
        report["cost_today"] = {
            "usd": spend["today"]["total_usd"],
            "budget_pct": spend["today"]["budget_pct_used"],
            "over_budget": spend["over_budget"],
        }
    except Exception:
        report["cost_today"] = {}

    report["generated_at"] = _t.time()
    return report


@app.get("/metrics/trends", tags=["metrics"])
def metrics_trends(hours: float = 24.0):
    """
    Trend analysis over the last N hours (default 24).
    Shows slope per metric — positive slope on error_rate = things getting worse.
    Includes alerts when thresholds are crossed.
    """
    return get_trends(hours=hours)


@app.get("/metrics/history", tags=["metrics"])
def metrics_history(hours: float = 48.0):
    """Return raw metric snapshots from the last N hours (default 48)."""
    from .learning.metrics_store import get_recent
    snaps = get_recent(hours=hours)
    return {"hours": hours, "count": len(snaps), "snapshots": snaps[-200:]}


@app.get("/credits/spend", tags=["meta"])
def credits_spend():
    """
    Token cost ledger — estimated API spend today, last 7 days, last 30 days.
    Set DAILY_BUDGET_USD env var to enable budget alerts (default $5).
    """
    return _spend_summary()


@app.get("/credits/breakdown", tags=["meta"])
def credits_breakdown():
    """
    Per-category API spend breakdown — daily and weekly.

    Shows exactly which autonomous functions are consuming credit:
      chat, auto_fix, code_review, voting, improvement, health_check, n8n, benchmark, other

    Also returns throttle tier and which jobs are currently active vs throttled.
    Set DAILY_BUDGET_USD in Railway Variables to enable tier-based throttling.
    """
    try:
        from .learning.pro_router import get_status as _pro_status
        pro = _pro_status()
    except Exception:
        pro = {"mode": "unknown", "pro_available": None}
    return {
        "breakdown": _cost_breakdown(),
        "throttle": _throttle_status(),
        "pro_subscription": pro,
        "note": (
            "Costs are estimates based on average token lengths per job type. "
            "Pro subscription is used first for all Claude calls — API key only "
            "activates when Pro limit is hit. "
            "Set DAILY_BUDGET_USD in Railway Variables to activate throttling. "
            "At <50% remaining: diff review paused, health_check LLM reduced. "
            "At <25%: voting/improvement/benchmark paused. "
            "At <10%: only n8n monitor + metrics snapshots run."
        ),
    }


@app.get("/credits/pro-status", tags=["meta"])
def credits_pro_status():
    """
    Returns current Pro subscription routing status using cached state.
    Uses get_status() (instant) instead of verify_pro_auth() (blocks 20-35s).
    Live verification happens automatically via health checks every 30 min.
    """
    try:
        from .learning.pro_router import get_status as _pro_status
        status = _pro_status()

        # Add Gemini CLI availability — use cached flag only, no subprocess
        try:
            from pathlib import Path as _Path
            _gemini_flag = _Path("/workspace/.gemini_down")
            status["gemini_available"] = not _gemini_flag.exists()
        except Exception:
            status["gemini_available"] = False

        # Add Anthropic API availability — check strike state (free, no API call)
        try:
            from .learning.agent_status_tracker import get_all_statuses
            workers = get_all_statuses()
            # If Haiku/Sonnet/Opus are all on strike, API has no credits
            api_workers = [w for w in workers if w["id"] in ("Anthropic Haiku", "Sonnet Anthropic", "Opus Anthropic")]
            all_strike = api_workers and all(w["state"] == "strike" for w in api_workers)
            status["api_available"] = not all_strike
        except Exception:
            status["api_available"] = False

        # Add per-CLI recovery metrics for dashboard display
        try:
            _wmap = {w["id"]: w for w in workers}
            for _cli, _key in [("Claude CLI Pro", "claude"), ("Gemini CLI", "gemini")]:
                _w = _wmap.get(_cli, {})
                status[f"{_key}_last_recovery_at"]     = _w.get("last_recovery_at")
                status[f"{_key}_last_recovery_layer"]  = _w.get("last_recovery_layer", "")
                status[f"{_key}_recovery_count_today"] = _w.get("recovery_count_today", 0)
        except Exception:
            pass

        # Expose login rate-limit state so dashboard can show cooldown countdown
        try:
            from .learning.cli_auto_login import _check_ratelimit
            _rl_remaining = _check_ratelimit()
            status["login_ratelimit_remaining_s"] = max(0, int(_rl_remaining))
            status["login_ratelimited"] = _rl_remaining > 0
        except Exception:
            status["login_ratelimit_remaining_s"] = 0
            status["login_ratelimited"] = False

        return status
    except Exception as e:
        return {"error": str(e)}


@app.post("/credits/pro-reset", tags=["meta"])
def credits_pro_reset():
    """Manually reset the Pro exhaustion/burst flag (POST version)."""
    try:
        from .learning.pro_router import reset_pro_flag
        reset_pro_flag()
        return {"ok": True, "message": "Pro flag cleared — Claude CLI is now primary again."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/credits/pro-reset", tags=["meta"])
def credits_pro_reset_get():
    """Manually reset the Pro exhaustion/burst flag — browser-friendly GET version."""
    try:
        from .learning.pro_router import reset_pro_flag
        reset_pro_flag()
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#4ade80'>✅ Pro flag cleared!</h2>"
            "<p style='font-family:sans-serif'>Claude CLI is now the primary model again.<br>"
            "<a href='/'>← Back to Super Agent</a></p>"
        )
    except Exception as e:
        return HTMLResponse(f"<h2 style='color:red'>Error: {e}</h2>")


@app.post("/webhook/verification-code", tags=["auth"])
@limiter.limit("10/minute")
def webhook_verification_code(payload: dict, request: Request):
    """
    Receive magic link URL (or 6-digit code) from n8n for automated Claude CLI re-login.

    Claude.ai passwordless auth sends a MAGIC LINK email, not a 6-digit code.
    n8n monitors the Hotmail inbox, extracts the magic link URL from the email,
    and POSTs it here so Playwright can navigate to it and complete the auth.

    Expected payload: {"url": "https://claude.ai/..."} or legacy {"code": "123456"}
    """
    # Accept magic link URL (new) or legacy 6-digit code
    auth_payload = (
        str(payload.get("url", "")).strip()
        or str(payload.get("code", "")).strip()
    )
    if not auth_payload:
        raise HTTPException(status_code=400, detail="payload must include 'url' or 'code' field")
    try:
        from .learning.cli_auto_login import receive_verification_code
        receive_verification_code(auth_payload)
        preview = auth_payload[:40] + "..." if len(auth_payload) > 40 else auth_payload
        return {"ok": True, "received": preview}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to relay auth payload: {e}")



@app.post("/webhook/refresh-cli-token", tags=["auth"])
@limiter.limit("10/minute")
def webhook_refresh_cli_token(payload: dict, request: Request):
    """
    Receive a fresh Claude CLI token from inspiring-cat after Playwright auto-login.

    When Playwright runs on inspiring-cat and obtains a new OAuth token, it pushes
    the fresh base64-encoded credentials here so super-agent can:
      1. Write them to /root/.claude/.credentials.json (local claude fallback path)
      2. Update os.environ["CLAUDE_SESSION_TOKEN"] in-memory (no redeploy needed)
      3. Clear the CLI_DOWN flag so the next request uses CLI again

    This bridges the gap between inspiring-cat's volume (where Playwright writes)
    and super-agent's local claude binary (which reads from its own /root/.claude/).

    Protected by N8N_API_KEY or GITHUB_PAT.
    Expected payload: {"token_b64": "<base64-encoded credentials.json>", "api_key": "..."}
    """
    import os as _os
    import base64 as _b64
    from pathlib import Path as _Path

    # Auth check — same pattern as store-memory
    valid_keys = {k for k in [
        _os.environ.get("N8N_API_KEY", ""),
        _os.environ.get("GITHUB_PAT", ""),
    ] if k}
    if not valid_keys or payload.get("api_key", "") not in valid_keys:
        raise HTTPException(status_code=403, detail="Invalid api_key")

    token_b64 = str(payload.get("token_b64", "")).strip()
    if not token_b64:
        raise HTTPException(status_code=400, detail="payload must include 'token_b64' field")

    try:
        decoded = _b64.b64decode(token_b64 + "==")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 token: {e}")

    # Sanity check — must look like a JSON credentials file
    if not decoded.startswith(b"{"):
        raise HTTPException(status_code=400, detail="Decoded token does not look like JSON credentials")

    # Write to all credential paths
    _cred_dir = _Path("/root/.claude")
    _cred_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for fpath in [
        _cred_dir / ".credentials.json",
        _cred_dir / "credentials.json",
        _Path("/root/.claude.json"),
    ]:
        try:
            fpath.write_bytes(decoded)
            fpath.chmod(0o600)
            written += 1
        except Exception:
            pass

    # Also write volume backup so entrypoint.sh picks it up on next restart
    try:
        _vol = _Path("/workspace/.claude_credentials_backup.json")
        _vol.write_bytes(decoded)
        _vol.chmod(0o600)
    except Exception:
        pass

    # Update in-memory env var so _try_restore_claude_auth() finds it immediately
    _os.environ["CLAUDE_SESSION_TOKEN"] = token_b64

    # Clear ALL routing flags — Playwright is a full recovery, BURST included.
    # clear_cli_down_flag() only clears CLI_DOWN; reset_pro_flag() also clears
    # BURST (30-min flag set when inspiring-cat returns token/credit errors).
    # Without clearing BURST, super-agent skips inspiring-cat for 30 min and
    # falls to its own broken local CLI even after Playwright heals the token.
    try:
        from .learning.pro_router import reset_pro_flag
        reset_pro_flag()
    except Exception:
        pass

    # Mark dashboard healthy
    try:
        from .learning.agent_status_tracker import mark_done as _md
        _md("Claude CLI Pro")
    except Exception:
        pass

    try:
        from .activity_log import bg_log
        bg_log("✅ CLI token pushed from inspiring-cat — credentials refreshed in-memory, CLI_DOWN cleared.", source="refresh_cli_token")
    except Exception:
        pass

    return {"ok": True, "paths_written": written, "message": "Token refreshed in super-agent memory and disk"}


@app.post("/webhook/store-memory", tags=["memory"])
def webhook_store_memory(payload: dict):
    """
    Seed a memory directly into the agent's long-term memory store.
    Protected by N8N_API_KEY — internal use only (n8n workflows, CI scripts).

    Expected payload:
      {"content": "...", "session_id": "seed", "api_key": "<N8N_API_KEY>"}
    Optional: {"memory_type": "fact", "importance": 4}   (defaults: general, 3)
    """
    import os as _os
    # Accept either N8N_API_KEY or GITHUB_PAT as the auth key
    valid_keys = {k for k in [
        _os.environ.get("N8N_API_KEY", ""),
        _os.environ.get("GITHUB_PAT", ""),
    ] if k}
    if not valid_keys or payload.get("api_key", "") not in valid_keys:
        raise HTTPException(status_code=403, detail="Invalid api_key")
    content = str(payload.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=400, detail="'content' field is required")
    session_id = str(payload.get("session_id", "seed"))
    memory_type = str(payload.get("memory_type", "fact"))
    importance = int(payload.get("importance", 4))
    try:
        from .memory.vector_memory import store_enriched_memory
        store_enriched_memory(session_id, content, memory_type=memory_type, importance=importance)
        return {"ok": True, "stored": content[:80] + ("..." if len(content) > 80 else "")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory store failed: {e}")


@app.get("/benchmark/latest", tags=["benchmark"])
def benchmark_latest():
    """Return the most recent benchmark report (runs every Monday 01:00 UTC)."""
    from .learning.benchmark import get_latest_benchmark
    result = get_latest_benchmark()
    if result is None:
        return {"message": "No benchmark run yet — first run Monday 01:00 UTC, or POST /benchmark/run"}
    return result


@app.post("/benchmark/run", tags=["benchmark"])
@limiter.limit("2/hour")
def benchmark_run(request: Request):  # noqa: ARG001
    """
    Manually trigger a benchmark run — calls all route types, scores with Haiku.
    Takes 30-60 seconds. Returns the full report.
    """
    from .learning.benchmark import run_benchmark
    try:
        return run_benchmark()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Benchmark failed: {e}")


@app.get("/benchmark/list", tags=["benchmark"])
def benchmark_list():
    """List all available benchmark dates."""
    from .learning.benchmark import list_benchmark_dates
    return {"dates": list_benchmark_dates()}


@app.get("/build/recipes", tags=["build"])
def build_recipes_list():
    """Return all recorded successful build recipes."""
    from .learning.build_recipes import list_recipes
    return {"recipes": list_recipes()}


@app.get("/build/recipes/{project_name}", tags=["build"])
def build_recipe_get(project_name: str):
    """Return the build recipe for a specific project, including the full step list."""
    from .learning.build_recipes import get_recipe
    recipe = get_recipe(project_name)
    if recipe is None:
        return {"found": False, "project_name": project_name}
    return {"found": True, "recipe": recipe}


# ── Multimodal ────────────────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg"}


@app.post("/chat/image", tags=["agent"])
@limiter.limit("20/minute")
async def chat_image(
    request: Request,
    file: UploadFile = File(...),
    prompt: str = Form(default=""),
    session_id: str = Form(default="default"),
):
    """Analyse an image with Claude Vision."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported image type: {file.content_type}")
    image_bytes = await file.read()
    if len(image_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 5 MB)")
    response = ask_claude_vision(image_bytes, file.content_type, prompt)
    if not response.startswith("["):
        append_exchange(session_id, prompt or "[image]", response)
    return {"response": response, "model_used": "CLAUDE", "routed_by": "vision", "session_id": session_id}


@app.post("/chat/audio", tags=["agent"])
@limiter.limit("20/minute")
async def chat_audio(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(default="default"),
):
    """Transcribe audio with Gemini, then route the transcript through the agent."""
    if file.content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported audio type: {file.content_type}")
    audio_bytes = await file.read()
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio too large (max 10 MB)")
    transcript = transcribe_audio(audio_bytes, file.content_type)
    if transcript.startswith("["):
        return {"response": transcript, "model_used": "GEMINI", "routed_by": "transcription_error",
                "transcript": "", "session_id": session_id}
    result = dispatch(transcript)
    if not result["response"].startswith("["):
        append_exchange(session_id, transcript, result["response"])
    return {
        "response": result["response"],
        "model_used": result["model_used"],
        "routed_by": result["routed_by"],
        "transcript": transcript,
        "session_id": session_id,
    }
