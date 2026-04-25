from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import settings

log = logging.getLogger("legion.db")
_pool: AsyncConnectionPool | None = None


async def startup() -> None:
    global _pool
    if not settings.PG_DSN:
        log.warning("PG_DSN not set — Legion running in memory-only mode")
        return
    _pool = AsyncConnectionPool(conninfo=settings.PG_DSN, min_size=1, max_size=5, open=False)
    await _pool.open()
    log.info("PG pool opened")


async def shutdown() -> None:
    if _pool is not None:
        await _pool.close()


@asynccontextmanager
async def connection():
    if _pool is None:
        raise RuntimeError("PG pool not initialised")
    async with _pool.connection() as conn:
        yield conn


async def upsert_agent_health(
    agent_id: str,
    model_id: str,
    status: str,
    latency_ms: int | None,
    error: str | None,
) -> None:
    """
    UPSERT a row in agent_health. status='ok' resets consecutive_failures
    and updates last_ok_at; any other status increments the counter.
    Silently no-ops if PG isn't configured (memory-only mode).
    """
    if _pool is None:
        return
    is_ok = status == "ok"
    async with _pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO agent_health (
                agent_id, model_id, last_probe_at, last_ok_at,
                last_status, last_error, consecutive_failures, latency_ms
            ) VALUES (
                %s, %s, now(),
                CASE WHEN %s THEN now() ELSE NULL END,
                %s, %s,
                CASE WHEN %s THEN 0 ELSE 1 END,
                %s
            )
            ON CONFLICT (agent_id, model_id) DO UPDATE SET
                last_probe_at = now(),
                last_ok_at = CASE WHEN %s THEN now() ELSE agent_health.last_ok_at END,
                last_status = EXCLUDED.last_status,
                last_error = EXCLUDED.last_error,
                consecutive_failures = CASE
                    WHEN %s THEN 0
                    ELSE agent_health.consecutive_failures + 1
                END,
                latency_ms = EXCLUDED.latency_ms
            """,
            (
                agent_id, model_id or "",
                is_ok,
                status, (error or "")[:300],
                is_ok,
                latency_ms,
                is_ok,
                is_ok,
            ),
        )


async def fetch_agent_health() -> list[dict]:
    """Return all rows from agent_health, ordered by agent_id."""
    if _pool is None:
        return []
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT agent_id, model_id, last_probe_at, last_ok_at,
                       last_status, last_error, consecutive_failures, latency_ms
                FROM agent_health
                ORDER BY agent_id, model_id
                """
            )
            rows = await cur.fetchall()
    return [
        {
            "agent_id": r[0],
            "model_id": r[1],
            "last_probe_at": r[2].isoformat() if r[2] else None,
            "last_ok_at": r[3].isoformat() if r[3] else None,
            "last_status": r[4],
            "last_error": r[5],
            "consecutive_failures": r[6],
            "latency_ms": r[7],
        }
        for r in rows
    ]


async def record_hive_round(
    round_id: str,
    query_hash: str,
    modality: str,
    agents_entered: list[str],
    winner: str | None,
    scores: dict[str, float],
    latency_ms: int,
    cost_cents: float,
) -> None:
    if _pool is None:
        return
    async with _pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO hive_rounds
                (round_id, query_hash, query_modality, agents_entered,
                 winner_agent, scores_json, latency_ms, cost_cents)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (round_id, query_hash, modality, agents_entered, winner,
             json.dumps(scores), latency_ms, cost_cents),
        )
