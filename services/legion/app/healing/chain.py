"""
Healing chain orchestrator. Runs L1 → L2 → L3 → L4 in order, stops at the
first success, records each attempt to claude_account_state.

L5 (DevBrowser-CDP) is planned for a separate phase and NOT chained here yet.
"""
from __future__ import annotations

import logging

from app.healing.l1_volume import restore_from_volume
from app.healing.l2_env import restore_from_env
from app.healing.l3_oauth import restore_from_refresh_token
from app.healing.l4_playwright import login_via_playwright
from app.state import AccountRole, record_healing_attempt, set_role

log = logging.getLogger("legion.healing.chain")


async def run_chain(account_id: str) -> str | None:
    """Return the name of the layer that succeeded, or None if all failed."""
    await set_role(account_id, AccountRole.HEALING, healing_layer="starting")

    # L1: volume backup
    if restore_from_volume():
        await record_healing_attempt(account_id, "L1", success=True)
        return "L1"
    await record_healing_attempt(account_id, "L1", success=False)

    # L2: env-var credentials blob
    if restore_from_env():
        await record_healing_attempt(account_id, "L2", success=True)
        return "L2"
    await record_healing_attempt(account_id, "L2", success=False)

    # L3: OAuth refresh_token (currently broken upstream)
    if restore_from_refresh_token():
        await record_healing_attempt(account_id, "L3", success=True)
        return "L3"
    await record_healing_attempt(account_id, "L3", success=False)

    # L4: Playwright + n8n magic-link (stub until P4)
    if await login_via_playwright():
        await record_healing_attempt(account_id, "L4", success=True)
        return "L4"
    await record_healing_attempt(account_id, "L4", success=False)

    log.warning("healing chain exhausted for account %s", account_id)
    await set_role(account_id, AccountRole.EXHAUSTED, healing_layer="exhausted_L1_L4")
    return None
