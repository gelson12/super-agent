"""
Shared-memory access for Legion's hive agents.

Hive agents share the super-agent unified memory store (PostgreSQL-backed
knowledge base reachable at $SUPER_AGENT_BASE_URL/memory/search). Before
each hive round we fetch the top-K most-relevant memories for the query
and prepend them to every agent's prompt — so the whole hive responds with
the same context inspiring-cat would have, even when CLI agents (claude,
gemini) can't see super-agent's local files.

Failure-safe: if super-agent is unreachable or returns nothing, we just
send the original query without context. The hive never blocks on memory
fetch.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("legion.memory")

DEFAULT_TIMEOUT_S = 1.5
DEFAULT_LIMIT = 5
DEFAULT_MIN_IMPORTANCE = 2


async def fetch_relevant(
    query: str,
    limit: int = DEFAULT_LIMIT,
    min_importance: int = DEFAULT_MIN_IMPORTANCE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """
    Hit super-agent's /memory/search endpoint. Returns a list of result dicts
    or [] on any error. Never raises.
    """
    base_url = os.environ.get(
        "SUPER_AGENT_BASE_URL",
        "https://super-agent-production.up.railway.app",
    ).rstrip("/")
    if not base_url:
        return []
    if not query or len(query.strip()) < 2:
        return []
    url = f"{base_url}/memory/search"
    params = {
        "q": query.strip()[:500],  # trim to keep URL reasonable
        "limit": str(limit),
        "min_importance": str(min_importance),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", []) if isinstance(data, dict) else []
    except Exception as exc:
        log.info("memory.fetch_relevant failed: %s — proceeding with no context",
                 type(exc).__name__)
        return []


def format_context_block(results: list[dict], max_chars: int = 2000) -> str:
    """
    Render a small context preamble that gets prepended to the user query.
    Caps total length so we don't blow agent token budgets.
    """
    if not results:
        return ""
    lines = ["[shared-memory context (most relevant first)]"]
    total = len(lines[0])
    for r in results:
        text = r.get("text") or r.get("content") or r.get("summary") or ""
        if not text:
            continue
        snippet = text.strip().replace("\n", " ")[:300]
        line = f"- {snippet}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if len(lines) == 1:
        return ""
    lines.append("[/context]")
    return "\n".join(lines) + "\n\n"


async def augment_query(query: str) -> str:
    """Convenience: fetch + format + prepend in one call."""
    results = await fetch_relevant(query)
    block = format_context_block(results)
    return block + query if block else query
