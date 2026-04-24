"""
L2: restore Claude credentials from a base64-encoded JSON blob in env var
CLAUDE_ACCOUNT_B_SESSION_TOKEN. Same shape as inspiring-cat's CLAUDE_SESSION_TOKEN.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("legion.healing.l2")

ACCOUNT_B_LIVE = Path("/workspace/legion/claude-b/credentials.json")


def restore_from_env(env_var: str = "CLAUDE_ACCOUNT_B_SESSION_TOKEN") -> bool:
    blob = os.environ.get(env_var, "").strip()
    if not blob:
        log.info("L2: %s not set", env_var)
        return False
    try:
        raw = base64.b64decode(blob)
    except binascii.Error as exc:
        log.warning("L2: base64 decode failed: %s", type(exc).__name__)
        return False
    try:
        json.loads(raw.decode("utf-8"))  # validate
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("L2: decoded payload invalid: %s", type(exc).__name__)
        return False
    try:
        ACCOUNT_B_LIVE.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNT_B_LIVE.write_bytes(raw)
        ACCOUNT_B_LIVE.chmod(0o600)
        log.info("L2: restored credentials from env var")
        return True
    except OSError as exc:
        log.warning("L2: write failed: %s", type(exc).__name__)
        return False
