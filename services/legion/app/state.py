"""
PostgreSQL state machine for Claude dual-account lifecycle.

Table: claude_account_state  (migrations/0001_legion_base.sql)

Coordination between inspiring-cat (account A) and legion (account B) happens
via row-level SELECT ... FOR UPDATE on the peer row, not advisory locks. This
avoids session-scoped-lock pitfalls with connection pooling and is simpler to
reason about.

All functions fail-safe when PG is unreachable or the table does not exist:
read functions return None and write functions log a warning — the service
stays serviceable (just not coordinated).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app import db

log = logging.getLogger("legion.state")


class AccountRole(str, Enum):
    ACTIVE = "active"
    PASSIVE = "passive"
    HEALING = "healing"
    EXHAUSTED = "exhausted"
    LOCKED = "locked"


@dataclass
class AccountState:
    account_id: str
    container: str
    role: AccountRole
    token_expires_at: datetime | None
    last_healed_at: datetime | None
    last_heartbeat: datetime
    exhaustion_count: int
    health_score: float
    healing_layer: str | None


_COLUMNS = (
    "account_id, container, role, token_expires_at, last_healed_at, "
    "last_heartbeat, exhaustion_count, health_score, healing_layer"
)


def _row_to_state(row) -> AccountState:
    return AccountState(
        account_id=row[0],
        container=row[1],
        role=AccountRole(row[2]),
        token_expires_at=row[3],
        last_healed_at=row[4],
        last_heartbeat=row[5],
        exhaustion_count=row[6],
        health_score=float(row[7]),
        healing_layer=row[8],
    )


async def get_account(account_id: str) -> AccountState | None:
    if db._pool is None:
        return None
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM claude_account_state WHERE account_id = %s",
                    (account_id,),
                )
                row = await cur.fetchone()
                return _row_to_state(row) if row else None
    except Exception as exc:
        log.warning("get_account(%s) failed: %s", account_id, type(exc).__name__)
        return None


async def heartbeat(account_id: str, container: str) -> None:
    if db._pool is None:
        return
    try:
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE claude_account_state "
                "SET last_heartbeat = NOW(), container = %s, updated_at = NOW() "
                "WHERE account_id = %s",
                (container, account_id),
            )
    except Exception as exc:
        log.warning("heartbeat(%s) failed: %s", account_id, type(exc).__name__)


async def set_role(
    account_id: str,
    role: AccountRole,
    healing_layer: str | None = None,
) -> bool:
    """Unconditional role update (callers of this must hold coordination themselves)."""
    if db._pool is None:
        return False
    try:
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE claude_account_state "
                "SET role = %s, healing_layer = %s, updated_at = NOW() "
                "WHERE account_id = %s",
                (role.value, healing_layer, account_id),
            )
            return True
    except Exception as exc:
        log.warning("set_role(%s, %s) failed: %s", account_id, role, type(exc).__name__)
        return False


async def try_become_active(my_account_id: str, my_container: str) -> bool:
    """
    Atomically check the peer row and, if the peer is not currently ACTIVE,
    set my row to ACTIVE. Uses SELECT ... FOR UPDATE on the peer row so two
    containers racing will be serialised by PG. Returns True if promotion
    succeeded.
    """
    if db._pool is None:
        return False
    peer_id = "A" if my_account_id == "B" else "B"
    try:
        async with db.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT role FROM claude_account_state "
                        "WHERE account_id = %s FOR UPDATE",
                        (peer_id,),
                    )
                    peer = await cur.fetchone()
                    if peer and peer[0] == AccountRole.ACTIVE.value:
                        return False
                    await cur.execute(
                        "UPDATE claude_account_state "
                        "SET role = %s, container = %s, updated_at = NOW() "
                        "WHERE account_id = %s",
                        (AccountRole.ACTIVE.value, my_container, my_account_id),
                    )
                    return True
    except Exception as exc:
        log.warning("try_become_active(%s) failed: %s", my_account_id, type(exc).__name__)
        return False


async def demote(account_id: str, to_role: AccountRole = AccountRole.PASSIVE) -> None:
    await set_role(account_id, to_role)


async def record_healing_attempt(account_id: str, layer: str, success: bool) -> None:
    if db._pool is None:
        return
    try:
        async with db.connection() as conn:
            if success:
                await conn.execute(
                    "UPDATE claude_account_state "
                    "SET last_healed_at = NOW(), healing_layer = %s, "
                    "health_score = LEAST(1.0, health_score + 0.1), "
                    "updated_at = NOW() WHERE account_id = %s",
                    (layer, account_id),
                )
            else:
                await conn.execute(
                    "UPDATE claude_account_state "
                    "SET healing_layer = %s, "
                    "health_score = GREATEST(0.0, health_score - 0.1), "
                    "exhaustion_count = exhaustion_count + 1, "
                    "updated_at = NOW() WHERE account_id = %s",
                    (layer, account_id),
                )
    except Exception as exc:
        log.warning("record_healing_attempt failed: %s", type(exc).__name__)
