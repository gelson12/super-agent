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
from .storage.cloudinary_manager import get_storage_status, upload_file
from .models.claude import ask_claude_vision
from .models.gemini import transcribe_audio
from .config import settings
from .cache.response_cache import cache
from .learning.insight_log import insight_log
from .learning.adapter import adapter
from .learning.wisdom_store import wisdom_store
# algorithm_store and algorithm_builder are imported lazily inside endpoints
# to avoid any startup-time blocking that could cause Railway health check failures

limiter = Limiter(key_func=get_remote_address)

# ── Auth middleware ────────────────────────────────────────────────────────────
# Protected paths require header: X-Token: <UI_PASSWORD>
# If UI_PASSWORD is not set, auth is disabled.

_OPEN_PATHS = {"/", "/health", "/auth"}
_OPEN_PREFIXES = ("/static",)


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
    Runs every 2 hours — full autonomous infrastructure health check across ALL services.
    Self-improve agent investigates and fixes SAFE issues without user intervention.
    """
    try:
        from .routing.dispatcher import dispatch as _dispatch
        _dispatch(
            "SCHEDULED HEALTH CHECK — investigate ALL services autonomously:\n"
            "1. railway_get_deployment_status + railway_get_logs — is everything deployed?\n"
            "2. db_health_check + db_get_failure_patterns — DB healthy? Any recurring errors?\n"
            "3. n8n_list_workflows — is n8n reachable? How many active workflows?\n"
            "4. run_shell_command('supervisorctl status') — are nginx, uvicorn, code-server running?\n"
            "5. run_shell_command('curl -s http://127.0.0.1:3001/') — is VS Code reachable?\n"
            "6. db_get_error_stats — which models are failing most?\n"
            "Auto-fix any SAFE issues found. Log what you checked and what state everything is in.",
            session_id="scheduler",
        )
    except Exception:
        pass  # Never let scheduler crash the process


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from apscheduler.schedulers.background import BackgroundScheduler
    from .learning.nightly_review import run_nightly_review
    from .learning.weekly_review import run_weekly_review
    from .learning.improvement_monitor import tick as _monitor_tick
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _scheduled_health_check,
        "interval",
        hours=2,
        id="health_check",
        replace_existing=True,
    )
    scheduler.add_job(
        run_nightly_review,
        "cron",
        hour=23,
        minute=0,
        id="nightly_review",
        replace_existing=True,
    )
    scheduler.add_job(
        run_weekly_review,
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
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str = Field(default="default", max_length=128)


class DirectChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
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
    message: str = Field(..., min_length=1, max_length=4000)
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
        # Run the full dispatcher (agents, tools, routing) synchronously,
        # then SSE the complete response in word-boundary chunks.
        try:
            result = dispatch(msg, session_id=req.session_id)
        except Exception as _dispatch_err:
            def _generate_dispatch_err():
                err = str(_dispatch_err)[:300].replace(chr(10), " ")
                yield f"data: Agent encountered an error: {err}\n\n"
                yield "data: [META:ERROR·dispatch_error·0]\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_generate_dispatch_err(), media_type="text/event-stream", headers=_SSE_HEADERS)

        response_text = result["response"]
        if not response_text.startswith("["):
            append_exchange(req.session_id, msg, response_text)

        def _generate_routed():
            # Normalize: replace bare newlines with space+newline+space so that
            # words on adjacent lines don't merge (e.g. "This\nis" → "This \n is").
            # Then split on spaces, filter empty, join with a space in the buffer.
            # Newlines are encoded as \n literals for SSE transport; frontend decodes them.
            normalized = response_text.replace("\n", " \n ")
            words = [w for w in normalized.split(" ") if w]
            buf = ""
            for word in words:
                buf += word + " "
                if len(buf) >= 60:
                    chunk = buf.rstrip().replace(chr(10), chr(92) + "n")
                    yield f"data: {chunk}\n\n"
                    buf = ""
            if buf.strip():
                yield f"data: {buf.rstrip().replace(chr(10), chr(92) + 'n')}\n\n"
            # Send routing metadata + memory count for the UI label
            _model = result.get("model_used", "AGENT")
            _route = result.get("routed_by", "dispatcher")
            _mem = result.get("memory_count", 0)
            yield f"data: [META:{_model}·{_route}·{_mem}]\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_generate_routed(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # Conversational — stream token-by-token with capabilities-aware + memory system prompt
    from .memory.vector_memory import get_memory_context as _get_mem, store_memory as _store_mem
    _caps = build_capabilities_block(settings)
    _mem_ctx = _get_mem(msg)
    _learned = _adapter.get_learned_context() or ""
    # Prepend cross-session memories so the model always sees past context
    if _mem_ctx:
        _learned = _mem_ctx + "\n" + _learned
    system = SYSTEM_PROMPT_CLAUDE.format(
        capabilities=_caps,
        learned_context=_learned,
    )

    client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)
    _mem_count = _mem_ctx.count("\n-") if _mem_ctx else 0

    def _generate():
        full_response = []
        try:
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=settings.max_tokens_claude,
                system=system,
                messages=[{"role": "user", "content": msg}],
            ) as stream:
                for text in stream.text_stream:
                    full_response.append(text)
                    # Encode newlines as \n literal — frontend decodes back to real newlines
                    yield f"data: {text.replace(chr(10), chr(92) + 'n')}\n\n"
            # Persist the full exchange to long-term memory and session history
            if full_response:
                _complete = "".join(full_response)
                _store_mem(req.session_id, f"Q: {msg[:300]} A: {_complete[:300]}")
                append_exchange(req.session_id, msg, _complete)
            yield f"data: [META:CLAUDE·conversational·{_mem_count}]\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: [Stream error: {e}]\n\n"
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
def build_stream():
    """
    SSE stream of the current Flutter build progress.
    Tails /workspace/build_progress.log in real-time — subscribe while a build is running
    to see exactly what step is executing instead of a blank 'Thinking...' spinner.
    Closes automatically when the build finishes (detects '🏁 Build pipeline complete').
    """
    from .tools.flutter_tools import BUILD_PROGRESS_LOG
    import time as _time

    _SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    def _tail():
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
                                yield f"data: {line.strip().replace(chr(10), ' ')}\n\n"
                        if "Build pipeline complete" in new_lines or "FAILED" in new_lines:
                            yield "data: [BUILD_STREAM_END]\n\n"
                            return
            except Exception:
                pass
            _time.sleep(1)
        yield "data: [BUILD_STREAM_TIMEOUT]\n\n"

    return StreamingResponse(_tail(), media_type="text/event-stream", headers=_SSE_HEADERS)


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

class LeadRequest(BaseModel):
    name: str = ""
    email: str = ""
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
    """Store a contact form submission from the Bridge commercial website."""
    import json as _json
    from pathlib import Path as _Path
    leads_file = _Path("/workspace/bridge_leads.jsonl")
    try:
        with leads_file.open("a") as f:
            f.write(_json.dumps(req.model_dump()) + "\n")
    except Exception:
        pass  # graceful fallback if /workspace not writable
    return {"ok": True}


@app.post("/newsletter", tags=["website"])
def submit_newsletter(req: NewsletterRequest):
    """Store a newsletter signup from the Bridge commercial website."""
    import json as _json
    from pathlib import Path as _Path
    nl_file = _Path("/workspace/bridge_newsletter.jsonl")
    try:
        with nl_file.open("a") as f:
            f.write(_json.dumps(req.model_dump()) + "\n")
    except Exception:
        pass
    return {"ok": True}


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
