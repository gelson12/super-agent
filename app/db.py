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
