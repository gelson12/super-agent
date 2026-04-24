from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from app import __version__, db
from app.agents.chatgpt import ChatGPTAgent
from app.agents.claude_b import ClaudeBAgent
from app.agents.gemini_b import GeminiBAgent
from app.agents.hf import HFAgent
from app.agents.kimi import KimiAgent
from app.agents.ollama import OllamaAgent
from app.auth import require_hmac
from app.beacon import router as beacon_router
from app.config import settings
from app.hive_engine import LegionExhausted, run_round
from app.models import RespondRequest, RespondResponse
from app.redact import install_root_filter
from app.webhook import router as webhook_router

install_root_filter()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("legion")

_STARTED_AT = time.time()
_AGENTS: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.startup()
    _AGENTS["kimi"] = KimiAgent()
    _AGENTS["gemini_b"] = GeminiBAgent()
    _AGENTS["ollama"] = OllamaAgent()
    _AGENTS["hf"] = HFAgent()
    _AGENTS["claude_b"] = ClaudeBAgent()
    _AGENTS["chatgpt"] = ChatGPTAgent()
    enabled = [aid for aid, a in _AGENTS.items() if getattr(a, "enabled", False)]
    log.info(
        "legion started: LEGION_ENABLED=%s, registered=%s, enabled=%s",
        settings.LEGION_ENABLED, list(_AGENTS), enabled,
    )
    yield
    await db.shutdown()


app = FastAPI(title="Legion Engineer", version=__version__, lifespan=lifespan)
app.include_router(beacon_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "uptime_s": int(time.time() - _STARTED_AT),
        "legion_enabled": settings.LEGION_ENABLED,
        "l5_enabled": settings.L5_ENABLED,
        "dual_account_enabled": settings.DUAL_ACCOUNT_ENABLED,
    }


@app.get("/health/detailed")
def health_detailed() -> dict:
    return {
        **health(),
        "agents": {aid: getattr(a, "enabled", False) for aid, a in _AGENTS.items()},
        "pg_configured": bool(settings.PG_DSN),
    }


@app.post("/v1/respond", response_model=RespondResponse, dependencies=[Depends(require_hmac)])
async def respond(req: RespondRequest) -> RespondResponse:
    if not settings.LEGION_ENABLED:
        raise HTTPException(status_code=503, detail="legion disabled")
    if not _AGENTS:
        raise HTTPException(status_code=503, detail="no agents registered")
    try:
        return await run_round(req, _AGENTS)
    except LegionExhausted as exc:
        raise HTTPException(status_code=502, detail=str(exc))
