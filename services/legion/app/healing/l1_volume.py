"""
L1: restore Claude credentials from the persistent Railway volume backup.
Writes to the account-scoped path so Account B never touches Account A's file.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("legion.healing.l1")

ACCOUNT_B_BACKUP = Path("/workspace/legion/claude-b/credentials-backup.json")
ACCOUNT_B_LIVE = Path("/workspace/legion/claude-b/credentials.json")


def restore_from_volume() -> bool:
    if not ACCOUNT_B_BACKUP.exists():
        log.info("L1: no backup file at %s", ACCOUNT_B_BACKUP)
        return False
    try:
        text = ACCOUNT_B_BACKUP.read_text(encoding="utf-8")
        json.loads(text)  # validate JSON
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("L1: backup unreadable/invalid: %s", type(exc).__name__)
        return False
    try:
        ACCOUNT_B_LIVE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ACCOUNT_B_BACKUP, ACCOUNT_B_LIVE)
        ACCOUNT_B_LIVE.chmod(0o600)
        log.info("L1: restored credentials from volume backup")
        return True
    except OSError as exc:
        log.warning("L1: copy failed: %s", type(exc).__name__)
        return False
