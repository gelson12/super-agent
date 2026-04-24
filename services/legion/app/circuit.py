"""
Per-agent circuit breaker. Tracks rolling error count and trips an agent
OPEN after too many consecutive failures so the hive engine stops fanning
out to it until a cooldown passes. On cooldown expiry the breaker moves
to HALF_OPEN and admits a single probe request; success closes the
breaker, failure re-opens it with a fresh cooldown.

State is in-memory only. Coordination across Legion replicas is out of
scope at P6.1 — a single-replica service doesn't need it. Multi-replica
deployments in the future can either accept per-replica breakers (eventual
convergence) or back this with Redis. Default cooldowns are read from
legion_config.yaml (circuit.cooldown_s) with sane fallbacks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from app.config_loader import load_config

log = logging.getLogger("legion.circuit")


class CircuitState(str, Enum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


@dataclass
class _AgentBreaker:
    agent_id: str
    state: CircuitState = CircuitState.CLOSED
    consecutive_errors: int = 0
    opened_at: float = 0.0
    cooldown_s: int = 60
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_breakers: dict[str, _AgentBreaker] = {}
_registry_lock = asyncio.Lock()


async def _get_breaker(agent_id: str) -> _AgentBreaker:
    b = _breakers.get(agent_id)
    if b is not None:
        return b
    async with _registry_lock:
        b = _breakers.get(agent_id)
        if b is None:
            cfg = load_config()
            cooldown = cfg.circuit_cooldown_s.get(agent_id, 60)
            b = _AgentBreaker(agent_id=agent_id, cooldown_s=cooldown)
            _breakers[agent_id] = b
        return b


async def allow(agent_id: str) -> bool:
    """
    Return True if the agent is permitted to run. OPEN breakers return
    False until cooldown expires; HALF_OPEN admits exactly one probe
    (subsequent allow() calls return False until record_success/failure).
    """
    b = await _get_breaker(agent_id)
    async with b.lock:
        if b.state is CircuitState.CLOSED:
            return True
        if b.state is CircuitState.OPEN:
            if time.monotonic() - b.opened_at >= b.cooldown_s:
                b.state = CircuitState.HALF_OPEN
                log.info("circuit[%s]: OPEN -> HALF_OPEN (probe admitted)", agent_id)
                return True
            return False
        # HALF_OPEN — one probe already out; block others until it resolves.
        return False


async def record_success(agent_id: str) -> None:
    b = await _get_breaker(agent_id)
    async with b.lock:
        if b.state is not CircuitState.CLOSED:
            log.info("circuit[%s]: %s -> CLOSED (probe succeeded)", agent_id, b.state.value)
        b.state = CircuitState.CLOSED
        b.consecutive_errors = 0


async def record_failure(agent_id: str) -> None:
    cfg = load_config()
    threshold = cfg.circuit_error_threshold
    b = await _get_breaker(agent_id)
    async with b.lock:
        b.consecutive_errors += 1
        if b.state is CircuitState.HALF_OPEN:
            b.state = CircuitState.OPEN
            b.opened_at = time.monotonic()
            log.info("circuit[%s]: HALF_OPEN -> OPEN (probe failed, cooldown %ds)",
                     agent_id, b.cooldown_s)
            return
        if b.state is CircuitState.CLOSED and b.consecutive_errors >= threshold:
            b.state = CircuitState.OPEN
            b.opened_at = time.monotonic()
            log.info("circuit[%s]: CLOSED -> OPEN after %d consecutive errors (cooldown %ds)",
                     agent_id, b.consecutive_errors, b.cooldown_s)


async def snapshot() -> dict[str, dict]:
    """Expose all breaker states for dashboards / metrics."""
    out: dict[str, dict] = {}
    for agent_id, b in _breakers.items():
        async with b.lock:
            out[agent_id] = {
                "state": b.state.value,
                "consecutive_errors": b.consecutive_errors,
                "opened_at": b.opened_at,
                "cooldown_s": b.cooldown_s,
            }
    return out


async def reset(agent_id: str | None = None) -> None:
    """Test helper — force-close a single breaker or all of them."""
    if agent_id is not None:
        b = await _get_breaker(agent_id)
        async with b.lock:
            b.state = CircuitState.CLOSED
            b.consecutive_errors = 0
            b.opened_at = 0.0
        return
    for b in list(_breakers.values()):
        async with b.lock:
            b.state = CircuitState.CLOSED
            b.consecutive_errors = 0
            b.opened_at = 0.0
