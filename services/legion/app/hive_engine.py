from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid

from app import circuit, db
from app.agents.base import run_with_deadline
from app.beacon import primary_healthy
from app.config_loader import load_config
from app.memory_client import augment_query
from app.models import AgentResponse, RespondRequest, RespondResponse
from app.rank import AgentProfile, pick_winner
from app.suitability import classify, shortlist

log = logging.getLogger("legion.hive")


class LegionExhausted(RuntimeError):
    """Raised when no agent succeeded or all scored below the minimum."""


async def run_round(req: RespondRequest, agents: dict[str, object]) -> RespondResponse:
    cfg = load_config()
    round_id = str(uuid.uuid4())
    start = time.monotonic()

    # P3 failover guard: if inspiring-cat recently beaconed as healthy, defer.
    # The dispatcher in super-agent should have fronted the primary first and
    # only called us on fallback; this check catches misrouted traffic where
    # the primary is actually fine. Skipped when DUAL_ACCOUNT_ENABLED is off
    # so P0-P2 behaviour is unchanged.
    if os.environ.get("DUAL_ACCOUNT_ENABLED", "false").lower() == "true":
        if primary_healthy():
            raise LegionExhausted("primary_is_healthy_defer_upstream")

    if req.shortlist_override:
        candidates = [
            a for a in req.shortlist_override
            if a in agents and getattr(agents[a], "enabled", False)
        ]
    else:
        candidates = [aid for aid, agent in agents.items() if getattr(agent, "enabled", False)]

    if not candidates:
        raise LegionExhausted("no_agents_enabled")

    suitability_scores = await classify(req.query, candidates)
    prior = cfg.modality_priors.get(req.modality, {})
    for aid, bonus in prior.items():
        if aid in suitability_scores:
            suitability_scores[aid] = min(1.0, suitability_scores[aid] + bonus)

    picked = shortlist(
        suitability_scores,
        k=cfg.hive.shortlist_k,
        max_k=cfg.hive.shortlist_max,
    )

    # Filter by circuit breaker — skip OPEN agents, admit HALF_OPEN probes.
    entered: list[str] = []
    skipped_open: list[str] = []
    for aid in picked:
        if await circuit.allow(aid):
            entered.append(aid)
        else:
            skipped_open.append(aid)
    if skipped_open:
        log.info("hive: skipping OPEN-breaker agents: %s", skipped_open)

    if not entered:
        raise LegionExhausted(f"all_shortlisted_agents_circuit_open:{skipped_open}")

    # Pull shared-memory context from super-agent and prepend to the query
    # so every hive agent (CLI or API) sees the same KB inspiring-cat would.
    # Failure-safe: empty/unreachable memory → original query unchanged.
    augmented_query = await augment_query(req.query)

    deadlines = cfg.hive.deadlines_ms
    tasks: dict[str, asyncio.Task[AgentResponse]] = {}
    for aid in entered:
        agent_deadline = min(deadlines.get(aid, req.deadline_ms), req.deadline_ms)
        tasks[aid] = asyncio.create_task(
            run_with_deadline(agents[aid], augmented_query, agent_deadline)
        )

    responses: list[AgentResponse] = []
    early_terminated = False
    et_conf = cfg.hive.early_termination_confidence_min
    et_lat_frac = cfg.hive.early_termination_latency_fraction_max
    pending = set(tasks.values())

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            try:
                r = t.result()
            except asyncio.CancelledError:
                continue
            responses.append(r)
            if (
                r.success
                and r.self_confidence >= et_conf
                and r.latency_ms < et_lat_frac * req.deadline_ms
            ):
                for p in pending:
                    p.cancel()
                early_terminated = True
                pending = set()
                break

    # Update circuit breakers based on what came back.
    for r in responses:
        if r.success:
            await circuit.record_success(r.agent_id)
        else:
            await circuit.record_failure(r.agent_id)

    profiles = await _load_profiles(entered)
    winner, scores = pick_winner(
        responses,
        suitability_scores,
        profiles,
        cfg.weights,
        cfg.min_acceptable_score,
        cfg.cold_start_sample_threshold,
    )

    latency_ms = int((time.monotonic() - start) * 1000)
    query_hash = hashlib.sha256(req.query.encode()).hexdigest()[:32]

    try:
        await db.record_hive_round(
            round_id=round_id,
            query_hash=query_hash,
            modality=req.modality,
            agents_entered=entered,
            winner=winner.agent_id if winner else None,
            scores=scores,
            latency_ms=latency_ms,
            cost_cents=sum(r.cost_cents for r in responses if r.success),
        )
    except Exception as exc:
        log.warning("hive_rounds write failed: %s", type(exc).__name__)

    if winner is not None:
        try:
            await _update_profiles(winner.agent_id, entered, responses)
        except Exception as exc:
            log.warning("hive_agent_scores update failed: %s", type(exc).__name__)

    if winner is None:
        raise LegionExhausted(
            f"no_agent_scored_above_{cfg.min_acceptable_score}"
        )

    return RespondResponse(
        round_id=uuid.UUID(round_id),
        winner_agent=winner.agent_id,
        content=winner.content or "",
        latency_ms=latency_ms,
        agents_entered=entered,
        scores=scores,
        early_terminated=early_terminated,
    )


async def _load_profiles(agent_ids: list[str]) -> dict[str, AgentProfile]:
    profiles = {aid: AgentProfile(aid) for aid in agent_ids}
    if db._pool is None:
        return profiles
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT agent_id, rolling_win_rate, error_rate_7d, sample_count "
                    "FROM hive_agent_scores WHERE agent_id = ANY(%s)",
                    (agent_ids,),
                )
                async for row in cur:
                    aid, wr, er, sc = row
                    profiles[aid] = AgentProfile(
                        agent_id=aid,
                        rolling_win_rate=float(wr) if wr is not None else 0.5,
                        error_rate_7d=float(er) if er is not None else 0.0,
                        sample_count=int(sc) if sc is not None else 0,
                    )
    except Exception as exc:
        log.warning("profiles load failed: %s", type(exc).__name__)
    return profiles


async def _update_profiles(winner_id: str, entered: list[str], responses: list[AgentResponse]) -> None:
    if db._pool is None:
        return
    alpha = 0.1
    by_agent = {r.agent_id: r for r in responses}
    async with db.connection() as conn:
        for aid in entered:
            r = by_agent.get(aid)
            latency = r.latency_ms if r else 0
            won = 1.0 if aid == winner_id else 0.0
            await conn.execute(
                """
                INSERT INTO hive_agent_scores
                    (agent_id, rolling_win_rate, avg_latency_ms, sample_count, last_updated)
                VALUES (%s, %s, %s, 1, NOW())
                ON CONFLICT (agent_id) DO UPDATE SET
                  rolling_win_rate = hive_agent_scores.rolling_win_rate * (1 - %s) + %s * %s,
                  avg_latency_ms   = COALESCE(hive_agent_scores.avg_latency_ms, 0) * 0.9 + %s * 0.1,
                  sample_count     = hive_agent_scores.sample_count + 1,
                  last_updated     = NOW()
                """,
                (aid, won, latency, alpha, alpha, won, latency),
            )
