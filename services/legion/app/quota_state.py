"""
Per-agent / per-model quota and rate-limit state with TTL.

Used by agents that have a fallback chain of equivalent models so the
hive can transparently route around an exhausted model until its quota
window resets. State is persisted to a JSON file on the Legion volume
so an agent that just got 429'd doesn't get re-tried on every container
restart inside the same UTC day.

Two TTL conventions:
  * Daily-quota providers (e.g. Gemini free tier — 250/day on flash):
    mark exhausted until next 00:00 UTC.
  * Transient rate-limit providers (e.g. OpenRouter Venice 429): mark
    exhausted for a short window (default 10 min) so we still re-probe
    relatively quickly.

Failure-safe: any read/write error degrades to "no exhaustion known"
rather than crashing the agent path. Quota state is an optimisation,
not a correctness gate.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("legion.quota")

_STATE_FILE = os.environ.get(
    "LEGION_QUOTA_STATE_FILE",
    "/workspace/legion/state/quota.json",
)
_lock = threading.Lock()


def _now_ts() -> float:
    return time.time()


def _next_utc_midnight_ts() -> float:
    now = datetime.now(tz=timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.timestamp()


def _load() -> dict:
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("quota_state load failed: %s", type(exc).__name__)
        return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        log.warning("quota_state save failed: %s", type(exc).__name__)


def _key(agent_id: str, model: str) -> str:
    return f"{agent_id}::{model}"


def is_exhausted(agent_id: str, model: str) -> bool:
    """Return True iff (agent, model) is currently within an exhaustion window."""
    with _lock:
        data = _load()
        until = data.get(_key(agent_id, model), {}).get("until_ts", 0)
        return _now_ts() < until


def mark_exhausted_until_utc_midnight(agent_id: str, model: str, reason: str = "") -> None:
    """Used for daily-quota providers like Gemini free tier."""
    until = _next_utc_midnight_ts()
    _set(agent_id, model, until, reason)
    log.info(
        "quota[%s/%s]: exhausted until next UTC midnight (reason=%s)",
        agent_id, model, reason or "?",
    )


def mark_exhausted_for(agent_id: str, model: str, seconds: int, reason: str = "") -> None:
    """Used for short-term rate-limits like OpenRouter 429 'try again shortly'."""
    until = _now_ts() + max(int(seconds), 30)
    _set(agent_id, model, until, reason)
    log.info(
        "quota[%s/%s]: exhausted for %ds (reason=%s)",
        agent_id, model, seconds, reason or "?",
    )


def _set(agent_id: str, model: str, until_ts: float, reason: str) -> None:
    with _lock:
        data = _load()
        data[_key(agent_id, model)] = {
            "until_ts": until_ts,
            "reason": reason or "unknown",
            "set_at_ts": _now_ts(),
        }
        # Drop entries whose TTL already expired so the file doesn't grow.
        cutoff = _now_ts()
        data = {k: v for k, v in data.items() if v.get("until_ts", 0) > cutoff}
        _save(data)


def next_available_model(agent_id: str, chain: list[str]) -> str:
    """
    Walk `chain` in order and return the first model that isn't currently
    exhausted. If they're all exhausted, return chain[0] anyway — the
    caller will probably 429 again, but pinning to the first lets us
    surface the failure rather than silently swallowing it.
    """
    if not chain:
        raise ValueError("next_available_model requires non-empty chain")
    for m in chain:
        if not is_exhausted(agent_id, m):
            return m
    log.warning(
        "quota[%s]: all %d models exhausted in fallback chain — falling back to chain[0]",
        agent_id, len(chain),
    )
    return chain[0]


def snapshot() -> dict[str, dict]:
    """Expose current state for /health/detailed and prober dashboards."""
    with _lock:
        data = _load()
    out: dict[str, dict] = {}
    now = _now_ts()
    for k, v in data.items():
        until = v.get("until_ts", 0)
        if until <= now:
            continue
        out[k] = {
            "until_ts": until,
            "remaining_s": int(until - now),
            "reason": v.get("reason", ""),
        }
    return out
