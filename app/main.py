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
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

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
from .learning.algorithm_store import algorithm_store
from .learning.algorithm_builder import build_and_commit_algorithms

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


app = FastAPI(
    title="Super Agent Backend",
    description="Multi-model AI agent with semantic routing (Claude / Gemini / DeepSeek)",
    version="1.0.0",
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
    return {
        "store": algorithm_store.status(),
        "algorithms": algorithm_store.list_algorithms(),
    }


@app.post("/algorithms/build", tags=["algorithms"])
def build_algorithms():
    """
    Manually trigger a build of self-generated algorithms from the current
    wisdom store and insight log data.
    New algorithms are committed to the 'super-agent-algorithms' GitHub repo.
    Runs automatically every 200 interactions.
    """
    try:
        summary = build_and_commit_algorithms()
        return {"ok": True, **summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Algorithm build failed: {e}")


@app.get("/algorithms/reload", tags=["algorithms"])
def reload_algorithms():
    """
    Hot-reload algorithms from the GitHub repo without restarting.
    Useful after a manual commit or forced build.
    """
    try:
        algorithm_store._refresh()
        return {"ok": True, "store": algorithm_store.status()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reload failed: {e}")


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
