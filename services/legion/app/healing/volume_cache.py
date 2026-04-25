"""
D — per-agent volume cache for CLI subscription credentials.

For agents that authenticate via a CLI's on-disk credentials directory
(`~/.gemini/`, `~/.claude/`, `~/.kimi/`), keep a tar.gz snapshot on the
Legion volume at `/workspace/legion/<agent>/creds.tar.gz`. Two flows:

  * snapshot(agent_id, source_dirs): tar+gzip the source dirs to the
    cache file. Called from a successful auth path or from a watchdog
    after L4/L5 healing writes fresh creds.
  * restore_if_present(agent_id, target_root): if the cache file exists
    and the target paths inside it are missing on disk, extract back to
    the filesystem. Called on container startup before agent init so
    the CLI starts pre-authenticated.

This complements (not replaces) the env-var tar pattern in
healing.cli_creds:

  Order on boot (most recent wins):
    1. cli_creds.restore_all()  — pulls from env var (operator-provided)
    2. volume_cache.restore_all() — pulls from on-volume cache, only if
       the path isn't already populated (so a fresh env-var paste wins
       over a stale volume snapshot)

The volume is the durable source of truth across container restarts;
the env var is the bootstrap source on a brand-new volume.

API-key agents (groq, cerebras, etc.) have no on-disk creds, so D
doesn't apply to them — their key lives in env, period.
"""
from __future__ import annotations

import io
import logging
import os
import tarfile
from pathlib import Path

log = logging.getLogger("legion.healing.volume_cache")

CACHE_ROOT = "/workspace/legion"

# Per-agent: list of source directories whose contents we cache.
# Paths are absolute (matching what the CLI itself writes).
AGENT_PATHS: dict[str, list[str]] = {
    "gemini_b": ["/root/.gemini"],
    "claude_b": ["/root/.claude"],
    "kimi": ["/root/.kimi"],
}


def _cache_file(agent_id: str) -> str:
    return os.path.join(CACHE_ROOT, agent_id, "creds.tar.gz")


def snapshot(agent_id: str) -> bool:
    """
    Tar+gz the agent's credential directories to the volume cache file.
    Returns True on success, False if no source dirs exist or write fails.
    Safe to call repeatedly — each call overwrites the cache atomically.
    """
    sources = AGENT_PATHS.get(agent_id)
    if not sources:
        log.info("volume_cache[%s]: no AGENT_PATHS entry — nothing to snapshot", agent_id)
        return False
    existing = [s for s in sources if os.path.isdir(s)]
    if not existing:
        log.info("volume_cache[%s]: source dirs missing %s, skipping", agent_id, sources)
        return False
    cache_path = _cache_file(agent_id)
    tmp_path = cache_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with tarfile.open(tmp_path, "w:gz") as tf:
            for src in existing:
                tf.add(src, arcname=src.lstrip("/"))
        os.replace(tmp_path, cache_path)
        size = os.path.getsize(cache_path)
        log.info(
            "volume_cache[%s]: snapshot ok (%d bytes) sources=%s",
            agent_id, size, existing,
        )
        return True
    except Exception as exc:
        log.warning(
            "volume_cache[%s]: snapshot failed: %s",
            agent_id, type(exc).__name__,
        )
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def restore_if_present(agent_id: str, force: bool = False) -> bool:
    """
    Extract the volume cache back to disk if the live credential dir is
    missing (or `force=True`). Returns True if extraction happened.

    The 'live dir present' check protects against overwriting fresher
    credentials that were written by L1-L5 healing or env-var restore
    earlier in the boot sequence.
    """
    sources = AGENT_PATHS.get(agent_id)
    if not sources:
        return False
    cache_path = _cache_file(agent_id)
    if not os.path.isfile(cache_path):
        log.info("volume_cache[%s]: no cache file at %s", agent_id, cache_path)
        return False
    if not force and any(os.path.isdir(s) and os.listdir(s) for s in sources):
        log.info(
            "volume_cache[%s]: live creds already present, not restoring (use force=True to override)",
            agent_id,
        )
        return False
    try:
        with tarfile.open(cache_path, "r:gz") as tf:
            members = []
            for m in tf.getmembers():
                p = "/" + m.name.lstrip("./")
                if not any(p.startswith(s.rstrip("/") + "/") or p == s for s in sources):
                    log.warning(
                        "volume_cache[%s]: rejecting unsafe path %s",
                        agent_id, m.name,
                    )
                    return False
                members.append(m)
            tf.extractall("/", members=members)
        log.info(
            "volume_cache[%s]: restored %d members from %s",
            agent_id, len(members), cache_path,
        )
        return True
    except Exception as exc:
        log.warning(
            "volume_cache[%s]: restore failed: %s",
            agent_id, type(exc).__name__,
        )
        return False


def restore_all() -> dict[str, bool]:
    """
    Run restore_if_present for every agent that has AGENT_PATHS. Called
    from main.lifespan after env-var restore (cli_creds.restore_all),
    so env-var bootstraps win over stale volume snapshots.
    """
    return {agent_id: restore_if_present(agent_id) for agent_id in AGENT_PATHS}


def snapshot_all() -> dict[str, bool]:
    """
    Snapshot every agent whose creds are currently on disk. Useful from
    a watchdog/heal-success path or as a periodic safety-net.
    """
    return {agent_id: snapshot(agent_id) for agent_id in AGENT_PATHS}
