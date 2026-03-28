"""
Super Agent — FastAPI backend
Endpoints:
  GET  /health          — liveness check
  POST /chat            — route message to best model
  POST /chat/direct     — force a specific model
  GET  /history/{sid}   — retrieve session history
  DELETE /history/{sid} — clear session history
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from .routing.dispatcher import dispatch
from .memory.session import append_exchange, get_messages, clear_session

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Super Agent Backend",
    description="Multi-model AI agent with semantic routing (Claude / Gemini / DeepSeek)",
    version="1.0.0",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ── Request / Response schemas ──────────────────────────────────────────────

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


class HistoryMessage(BaseModel):
    role: str
    content: str


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    """Liveness check."""
    return {"ok": True, "version": "1.0.0"}


@app.post("/chat", response_model=ChatResponse, tags=["agent"])
@limiter.limit("30/minute")
def chat(req: ChatRequest, request: Request):
    """
    Route the message to the best model automatically via semantic classifier.
    Conversation history is saved per session_id.
    """
    result = dispatch(req.message)
    append_exchange(req.session_id, req.message, result["response"])
    return ChatResponse(
        response=result["response"],
        model_used=result["model_used"],
        routed_by=result["routed_by"],
        session_id=req.session_id,
    )


@app.post("/chat/direct", response_model=ChatResponse, tags=["agent"])
@limiter.limit("30/minute")
def chat_direct(req: DirectChatRequest, request: Request):
    """
    Force a specific model — skips the classifier.
    Useful for testing or when you already know the right model.
    """
    result = dispatch(req.message, force_model=req.model)
    if result["model_used"] is None:
        raise HTTPException(status_code=400, detail=result["response"])
    append_exchange(req.session_id, req.message, result["response"])
    return ChatResponse(
        response=result["response"],
        model_used=result["model_used"],
        routed_by=result["routed_by"],
        session_id=req.session_id,
    )


@app.get("/history/{session_id}", response_model=list[HistoryMessage], tags=["memory"])
def get_history(session_id: str):
    """Retrieve all messages for a session."""
    msgs = get_messages(session_id)
    return [
        HistoryMessage(role=m.type, content=m.content)
        for m in msgs
    ]


@app.delete("/history/{session_id}", tags=["memory"])
def delete_history(session_id: str):
    """Clear all messages for a session."""
    clear_session(session_id)
    return {"ok": True, "session_id": session_id, "cleared": True}
