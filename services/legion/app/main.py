from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from app import __version__, db, quota_state
from app.agents.cerebras import CerebrasAgent
from app.agents.chatgpt import ChatGPTAgent
from app.agents.claude_b import ClaudeBAgent
from app.agents.deepseek import DeepSeekAgent
from app.agents.gemini_b import GeminiBAgent
from app.agents.glm import GLMAgent
from app.agents.github_models import GitHubModelsAgent
from app.agents.groq import GroqAgent
from app.agents.hf import HFAgent
from app.agents.mistral import MistralAgent
from app.agents.ollama import OllamaAgent
from app.agents.openrouter import OpenRouterAgent
from app.agents.sambanova import SambaNovaAgent
from app.auth import require_hmac
from app.beacon import router as beacon_router
from app.config import settings
from app.healing.cli_creds import restore_all as restore_cli_creds
from app.healing.volume_cache import restore_all as restore_volume_cache
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
    # Restore CLI subscription credentials from base64 env blobs (Kimi,
    # Gemini-B, optional Claude-B tar) BEFORE agents initialise so their
    # subprocess lookups find the right session files on first call.
    try:
        creds_results = restore_cli_creds()
        log.info("cli_creds restore: %s", creds_results)
    except Exception as exc:
        log.warning("cli_creds restore failed: %s", type(exc).__name__)

    # Volume-cache restore runs AFTER env-var restore so a freshly-pasted
    # env blob always wins over a stale on-volume snapshot. Only restores
    # when the live creds dir is empty/missing (so it's a no-op when
    # cli_creds already populated everything).
    try:
        vol_results = restore_volume_cache()
        log.info("volume_cache restore: %s", vol_results)
    except Exception as exc:
        log.warning("volume_cache restore failed: %s", type(exc).__name__)

    try:
        await db.startup()
    except Exception as exc:
        log.error(
            "legion: DB startup failed (%s) — continuing without persistent storage; "
            "circuit breakers and ranking will use in-memory state only",
            type(exc).__name__,
        )
    _AGENTS["gemini_b"] = GeminiBAgent()
    _AGENTS["ollama"] = OllamaAgent()
    _AGENTS["hf"] = HFAgent()
    _AGENTS["claude_b"] = ClaudeBAgent()
    _AGENTS["chatgpt"] = ChatGPTAgent()
    _AGENTS["groq"] = GroqAgent()
    _AGENTS["openrouter"] = OpenRouterAgent()
    _AGENTS["cerebras"] = CerebrasAgent()
    _AGENTS["github_models"] = GitHubModelsAgent()
    _AGENTS["mistral"] = MistralAgent()
    _AGENTS["sambanova"] = SambaNovaAgent()
    _AGENTS["deepseek"] = DeepSeekAgent()
    _AGENTS["glm"] = GLMAgent()
    enabled = [aid for aid, a in _AGENTS.items() if getattr(a, "enabled", False)]
    if not settings.LEGION_ENABLED:
        log.warning(
            "LEGION_ENABLED=False — all /v1/respond requests will return 503. "
            "Set LEGION_ENABLED=true in Railway env vars to activate the hive."
        )
    if not enabled:
        log.warning(
            "legion started with zero enabled agents — all hive calls will fail. "
            "Check agent-specific env vars (GROQ_API_KEY, OPENROUTER_API_KEY, etc.)."
        )
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
async def health_detailed() -> dict:
    base = {
        **health(),
        "agents": {aid: getattr(a, "enabled", False) for aid, a in _AGENTS.items()},
        "pg_configured": bool(settings.PG_DSN),
        "quota_state": quota_state.snapshot(),
    }
    try:
        base["agent_health"] = await db.fetch_agent_health()
    except Exception as exc:
        log.warning("agent_health fetch failed: %s", type(exc).__name__)
        base["agent_health"] = []
    return base


@app.post("/v1/respond", response_model=RespondResponse, dependencies=[Depends(require_hmac)])
async def respond(req: RespondRequest) -> RespondResponse:
    if not settings.LEGION_ENABLED:
        raise HTTPException(status_code=503, detail="legion disabled")
    enabled_agents = [aid for aid, a in _AGENTS.items() if getattr(a, "enabled", False)]
    if not enabled_agents:
        raise HTTPException(status_code=503, detail="no agents enabled")
    try:
        return await run_round(req, _AGENTS)
    except LegionExhausted as exc:
        raise HTTPException(status_code=502, detail=str(exc))
