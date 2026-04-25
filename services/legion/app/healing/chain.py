"""
Healing chain orchestrator. Runs L1 → L2 → L3 → L4 → L5 in order, stops at
the first success, records each attempt to claude_account_state, and writes
per-attempt diagnostics.

L5 (DevBrowser-CDP) is gated on:
  * L5_ENABLED=true env flag
  * L4 returned a recoverable failure signature (not account_locked /
    invalid_credentials — those are terminal, we flip to LOCKED).

L3 OAuth is a stub (upstream broken since Mar 2026) kept in the sequence
for parity with inspiring-cat's layering.
"""
from __future__ import annotations

import logging
import os

from app.healing.l1_volume import restore_from_volume
from app.healing.l2_env import restore_from_env
from app.healing.l3_oauth import restore_from_refresh_token
from app.healing.l4_playwright import TERMINAL_SIGNATURES, login_via_playwright
from app.healing.l5_devbrowser import login_via_cdp
from app.healing.volume_cache import snapshot as snapshot_volume_cache
from app.state import AccountRole, record_healing_attempt, set_role

log = logging.getLogger("legion.healing.chain")


def _agent_id_for_account(account_id: str) -> str:
    """Map claude_account_state.account_id ('A'|'B') → AGENT_PATHS key."""
    return "claude_b" if account_id == "B" else "claude_a"


async def run_chain(account_id: str) -> str | None:
    await set_role(account_id, AccountRole.HEALING, healing_layer="starting")
    agent_key = _agent_id_for_account(account_id)

    if restore_from_volume():
        await record_healing_attempt(account_id, "L1", success=True)
        return "L1"
    await record_healing_attempt(account_id, "L1", success=False)

    if restore_from_env():
        # Fresh creds on disk — snapshot to volume so a future restart can
        # boot from the durable volume copy without re-decoding env.
        snapshot_volume_cache(agent_key)
        await record_healing_attempt(account_id, "L2", success=True)
        return "L2"
    await record_healing_attempt(account_id, "L2", success=False)

    if restore_from_refresh_token():
        snapshot_volume_cache(agent_key)
        await record_healing_attempt(account_id, "L3", success=True)
        return "L3"
    await record_healing_attempt(account_id, "L3", success=False)

    account_email = os.environ.get("CLAUDE_ACCOUNT_B_EMAIL", "")
    l4 = await login_via_playwright(account_email, account_id)
    if l4.diag is not None:
        l4.diag.write()
    if l4.success:
        snapshot_volume_cache(agent_key)
        await record_healing_attempt(account_id, "L4", success=True)
        return "L4"
    await record_healing_attempt(account_id, "L4", success=False)

    if l4.failure_signature in TERMINAL_SIGNATURES:
        log.info(
            "chain: L4 returned terminal signature %s — marking LOCKED, skipping L5",
            l4.failure_signature,
        )
        await set_role(
            account_id,
            AccountRole.LOCKED,
            healing_layer=f"locked_{l4.failure_signature}",
        )
        return None

    if os.environ.get("L5_ENABLED", "false").lower() != "true":
        log.info("chain: L5_ENABLED=false — not attempting CDP recovery")
    else:
        l5 = await login_via_cdp(account_email, l4.failure_signature, account_id)
        if l5.diag is not None:
            l5.diag.write()
        if l5.success:
            snapshot_volume_cache(agent_key)
            await record_healing_attempt(account_id, "L5", success=True)
            return "L5"
        await record_healing_attempt(account_id, "L5", success=False)

    log.warning("chain exhausted for account %s", account_id)
    await set_role(account_id, AccountRole.EXHAUSTED, healing_layer="exhausted")
    return None
