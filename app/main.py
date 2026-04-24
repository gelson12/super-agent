from __future__ import annotations

import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from app import __version__, db
from app.agents.base import run_with_deadline
from app.agents.kimi import KimiAgent
from app.auth import require_hmac
from app.config import settings
from app.models import RespondRequest, RespondResponse
from app.redact import install_root_filter

install_root_filter()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("legion")

_STARTED_AT = time.time()
_AGENTS: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.startup()
    _AGENTS["kimi"] = KimiAgent()
    log.info("legion started — enabled=%s agents=%s", settings.LEGION_ENABLED, list(_AGENTS))
    yield
    await db.shutdown()


app = FastAPI(title="Legion Engineer", version=__version__, lifespan=lifespan)


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

    shortlist = _shortlist(req)
    if not shortlist:
        raise HTTPException(status_code=503, detail="no agents available")

    start = time.monotonic()
    round_id = str(uuid.uuid4())
    # P1: single-agent round — Kimi only. Hive fan-out lands in P2.
    responses = []
    for agent_id in shortlist:
        agent = _AGENTS.get(agent_id)
        if agent is None:
            continue
        resp = await run_with_deadline(agent, req.query, req.deadline_ms)
        responses.append(resp)

    winners = [r for r in responses if r.success]
    if not winners:
        scores = {r.agent_id: 0.0 for r in responses}
        await _record(round_id, req, [r.agent_id for r in responses], None, scores, start, 0.0)
        raise HTTPException(status_code=502, detail="LegionExhausted")

    # P1 has only one active agent; winner = first successful response.
    winner = winners[0]
    scores = {r.agent_id: (1.0 if r is winner else 0.0) for r in responses}
    latency_ms = int((time.monotonic() - start) * 1000)
    await _record(round_id, req, [r.agent_id for r in responses], winner.agent_id, scores,
                  start, winner.cost_cents)

    return RespondResponse(
        round_id=uuid.UUID(round_id),
        winner_agent=winner.agent_id,
        content=winner.content or "",
        latency_ms=latency_ms,
        agents_entered=[r.agent_id for r in responses],
        scores=scores,
        early_terminated=False,
    )


def _shortlist(req: RespondRequest) -> list[str]:
    if req.shortlist_override:
        return [a for a in req.shortlist_override if a in _AGENTS]
    return [aid for aid, agent in _AGENTS.items() if getattr(agent, "enabled", False)]


async def _record(round_id: str, req: RespondRequest, entered: list[str],
                  winner: str | None, scores: dict[str, float],
                  start_monotonic: float, cost_cents: float) -> None:
    try:
        await db.record_hive_round(
            round_id=round_id,
            query_hash=hashlib.sha256(req.query.encode()).hexdigest()[:32],
            modality=req.modality,
            agents_entered=entered,
            winner=winner,
            scores=scores,
            latency_ms=int((time.monotonic() - start_monotonic) * 1000),
            cost_cents=cost_cents,
        )
    except Exception as exc:
        log.warning("hive_rounds write failed: %s", type(exc).__name__)
