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

_OPEN_PATHS = {"/", "/health", "/auth", "/credits/pro-status", "/credits/pro-reset"}
_OPEN_PREFIXES = ("/static", "/downloads", "/webhook", "/n8n/connection-info", "/activity")  # token-in-URL or public info


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
            "3. n8n_list_workflows — is n8n reachable?\n"
            "4. run_shell_command('supervisorctl status') — are all processes running?\n"
            "5. db_get_error_stats — which models/routes are failing most?\n"
            "Auto-fix any SAFE issues. Record findings in a brief internal log. "
            "Only notify the user if you find something critical that needs their input.",
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

    # CLI + n8n self-healing watchdog — every 5 min until resolved, then hourly re-check
    def _cli_n8n_watchdog_job():
        try:
            from .learning.cli_n8n_watchdog import run_watchdog_cycle
            run_watchdog_cycle()
        except Exception as _e:
            bg_log(f"CLI/n8n watchdog error: {_e}", source="cli_n8n_watchdog")

    scheduler.add_job(
        _cli_n8n_watchdog_job,
        "interval",
        minutes=5,
        id="cli_n8n_watchdog",
        replace_existing=True,
    )

    scheduler.start()
    yield
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, "version": "1.0.0"}


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
    result = dispatch(req.message, session_id=req.session_id)
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
        # ── 1. Claude CLI (zero cost) ─────────────────────────────────────────
        yield "data: [PROGRESS:🤔 Thinking… (est. 5–15s)]\n\n"
        try:
            from .learning.pro_router import try_pro, should_attempt_cli, drain_progress_events as _drain_conv
            if should_attempt_cli():
                yield "data: [PROGRESS:⚡ Using Claude CLI… (est. 5–15s)]\n\n"
                cli_resp = try_pro(f"{system}\n\n{msg}")
                # Surface any self-healing events that fired during the CLI call
                for _ev in _drain_conv():
                    yield f"data: [PROGRESS:{_ev}]\n\n"
                if cli_resp and not cli_resp.startswith("["):
                    _store_mem(req.session_id, f"Q: {msg[:300]} A: {cli_resp[:300]}")
                    append_exchange(req.session_id, msg, cli_resp)
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
            pass

        # ── 2. Gemini CLI (free fallback) ─────────────────────────────────────
        yield "data: [PROGRESS:🤖 Trying Gemini… (est. 5–10s)]\n\n"
        try:
            from .learning.gemini_cli_worker import ask_gemini_cli
            gemini_resp = ask_gemini_cli(msg)
            if gemini_resp and not gemini_resp.startswith("["):
                _store_mem(req.session_id, f"Q: {msg[:300]} A: {gemini_resp[:300]}")
                append_exchange(req.session_id, msg, gemini_resp)
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
            pass

        # ── 3. CLI and Gemini both unavailable — do not call Anthropic API ──────
        yield "data: [PROGRESS:⚠️ Claude CLI & Gemini both unavailable — please try again in a moment]\n\n"
        yield "data: ⚠️ Both Claude CLI and Gemini are temporarily unavailable. Please try again in a few seconds.\n\n"
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
    return wisdom_store.wisdom_dict()


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
def build_algorithms():
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


@app.post("/webhook/railway", tags=["webhook"])
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
def submit_lead(req: LeadRequest):
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
def submit_contact(req: ContactRequest):
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
def submit_newsletter(req: NewsletterRequest):
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

    # Watchdog alert — injected at front so it's always visible
    try:
        from .learning.cli_n8n_watchdog import read_watchdog_alert
        wa = read_watchdog_alert()
        if wa:
            report["watchdog_alert"] = wa
            # Also prepend to trend_alerts so existing UI picks it up
            report["trend_alerts"] = [wa] + report.get("trend_alerts", [])
    except Exception:
        pass

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
    Returns VERIFIED current Pro subscription routing status.
    Runs 'claude auth status' live — never returns assumed/stale state.
    mode=pro_primary        — Pro subscription active and verified
    mode=api_fallback_*     — Pro unavailable, using ANTHROPIC_API_KEY
    auth.pro_valid=true/false — actual verified auth result
    """
    try:
        from .learning.pro_router import get_status as _pro_status, verify_pro_auth as _verify
        # Live auth check first — syncs flags before reading status
        auth = _verify()
        status = _pro_status()
        status["auth"] = auth
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
