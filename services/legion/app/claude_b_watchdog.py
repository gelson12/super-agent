"""
Account B watchdog. Supervisord launches this as a separate long-running
process. Responsibilities:

  * Heartbeat claude_account_state row for account B every 30s.
  * When role==passive, proactively run the healing chain if the token is
    close to expiry or was never initialised.
  * When role==active (Legion took over from inspiring-cat), don't heal —
    the active path runs inside the request handler.

This module short-circuits cleanly if DUAL_ACCOUNT_ENABLED is false so it's
safe to leave in supervisord's autostart list once the flag flips.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from app import db
from app.healing.chain import run_chain
from app.redact import install_root_filter
from app.state import AccountRole, get_account, heartbeat

log = logging.getLogger("legion.watchdog")

ACCOUNT_ID = "B"
CONTAINER_NAME = "legion"
HEARTBEAT_EVERY_S = 30
HEAL_EVERY_S = 300  # one healing attempt window
HEAL_BEFORE_EXPIRY_S = 2 * 3600  # 2 hours


async def _maybe_heal(state) -> None:
    if state is None:
        return
    if state.role not in (AccountRole.PASSIVE, AccountRole.HEALING):
        return
    expires_at = state.token_expires_at
    now = datetime.now(tz=timezone.utc)
    needs_heal = (
        expires_at is None
        or (expires_at.tzinfo is not None and (expires_at - now).total_seconds() < HEAL_BEFORE_EXPIRY_S)
    )
    if not needs_heal:
        return
    log.info("watchdog: triggering healing chain for account %s", ACCOUNT_ID)
    layer = await run_chain(ACCOUNT_ID)
    if layer:
        log.info("watchdog: account %s healed via %s", ACCOUNT_ID, layer)
    else:
        log.warning("watchdog: account %s healing exhausted", ACCOUNT_ID)


async def _loop() -> None:
    await db.startup()
    try:
        last_heal_attempt = 0.0
        while True:
            await heartbeat(ACCOUNT_ID, CONTAINER_NAME)
            state = await get_account(ACCOUNT_ID)
            now = asyncio.get_event_loop().time()
            if now - last_heal_attempt >= HEAL_EVERY_S:
                await _maybe_heal(state)
                last_heal_attempt = now
            await asyncio.sleep(HEARTBEAT_EVERY_S)
    finally:
        await db.shutdown()


def main() -> None:
    install_root_filter()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if os.environ.get("DUAL_ACCOUNT_ENABLED", "false").lower() != "true":
        log.info("watchdog: DUAL_ACCOUNT_ENABLED=false — exiting cleanly")
        return
    log.info("watchdog: starting")
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        log.info("watchdog: shutdown")


if __name__ == "__main__":
    main()
