"""
L3: OAuth refresh_token grant. Currently a stub because Anthropic's OAuth
endpoint has been returning HTTP 405 for automated refresh since March 2026
(mirrors inspiring-cat's broken L3). Returns False without attempting.
"""
from __future__ import annotations

import logging

log = logging.getLogger("legion.healing.l3")


def restore_from_refresh_token() -> bool:
    log.info("L3 OAuth: skipped (upstream endpoint currently HTTP 405)")
    return False
