"""
Generic CLI-subscription credential bootstrap.

Pattern for each CLI-based agent (Kimi, Gemini-B, Claude-B):
  1. Operator logs in once inside a live Legion container via `railway ssh`,
     e.g. `kimi login` → browser/device-code flow completes.
  2. Operator captures the resulting creds dir as a base64 tar.gz blob:
       tar czf - <paths...> | base64 -w 0
  3. Operator pastes that blob into Railway as the service env var
     named here (KIMI_SESSION_TOKEN / GEMINI_B_SESSION_TOKEN / ...).
  4. On every container restart, restore_tarball() decodes and extracts
     so the CLI starts pre-authenticated. Idempotent — safe if already extracted.

Entrypoint hook in app.main.lifespan calls restore_all() before agents boot.
Failures are logged, never fatal — the agent stays disabled rather than the
whole service crashing.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import tarfile

log = logging.getLogger("legion.healing.cli_creds")


def restore_tarball(env_var: str, label: str) -> bool:
    """
    Decode env_var (base64 of a gzip'd tar) and extract its contents at
    filesystem root (`/`). Returns True on success, False on missing env var
    or any extraction error.

    Paths in the tar are treated as absolute. The user is expected to capture
    with `tar czf - /absolute/path/... | base64 -w 0` so the tar contains
    full paths that restore to the same locations on boot.
    """
    blob = os.environ.get(env_var, "").strip()
    if not blob:
        log.info("cli_creds[%s]: %s not set, skipping", label, env_var)
        return False
    try:
        raw = base64.b64decode(blob)
    except (binascii.Error, ValueError):
        log.warning("cli_creds[%s]: %s base64 decode failed", label, env_var)
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            # Guard against absolute-path escapes — each member must live
            # under the user's own bin/config/share dirs or Legion's workspace.
            safe_prefixes = (
                "/root/.local/share/",
                "/root/.config/",
                "/root/.gemini/",
                "/root/.claude/",
                "/root/.kimi/",
                "/workspace/legion/",
                "root/.local/share/",
                "root/.config/",
                "root/.gemini/",
                "root/.claude/",
                "root/.kimi/",
                "workspace/legion/",
            )
            members = []
            for m in tf.getmembers():
                p = m.name.lstrip("./")
                if not p.startswith(safe_prefixes):
                    log.warning("cli_creds[%s]: rejecting unsafe path %s", label, m.name)
                    return False
                members.append(m)
            tf.extractall("/", members=members)
        log.info("cli_creds[%s]: restored %d members from %s", label, len(members), env_var)
        return True
    except tarfile.TarError as exc:
        log.warning("cli_creds[%s]: tar extract failed: %s", label, type(exc).__name__)
        return False
    except OSError as exc:
        log.warning("cli_creds[%s]: filesystem write failed: %s", label, type(exc).__name__)
        return False


def restore_all() -> dict[str, bool]:
    """Restore all configured CLI credentials. Returns {label: success}."""
    results = {
        "kimi":     restore_tarball("KIMI_SESSION_TOKEN",     "kimi"),
        "gemini_b": restore_tarball("GEMINI_B_SESSION_TOKEN", "gemini_b"),
        # Claude-B already uses single-JSON L2 path in app.healing.l2_env.
        # If the operator prefers the tarball pattern for Claude too, set
        # CLAUDE_ACCOUNT_B_SESSION_TAR and it'll be used in addition.
        "claude_b": restore_tarball("CLAUDE_ACCOUNT_B_SESSION_TAR", "claude_b"),
    }
    return results
