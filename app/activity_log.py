"""
Shared background activity log — visible to users via GET /activity/stream and /activity/recent.

All autonomous background operations (health check, post-deploy check, Railway webhook,
n8n monitor, nightly review, weekly review) write here so the user can see what
Super Agent is doing in the background — just like the build progress log.

Usage:
    from .activity_log import bg_log
    bg_log("Health check: all services healthy")
    bg_log("Nightly review: applying LOW priority suggestion to app/tools/search_tools.py")
"""
import os
from datetime import datetime, timezone
from pathlib import Path

_LOG_DIR = Path("/workspace")
_FALLBACK_DIR = Path(".")

ACTIVITY_LOG: Path = (
    _LOG_DIR / "agent_activity.log"
    if os.access(_LOG_DIR, os.W_OK)
    else _FALLBACK_DIR / "agent_activity.log"
)

# Maximum lines to keep in the log file before trimming (keeps file manageable)
_MAX_LINES = 2000
_TRIM_TO = 1500


def bg_log(msg: str, source: str = "") -> None:
    """
    Append a timestamped line to the shared background activity log.

    Args:
        msg:    The message to log (will be truncated to 400 chars).
        source: Optional label like 'health_check', 'nightly_review', etc.
                If omitted the caller's module name is used if detectable.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        label = f"[{source}] " if source else ""
        line = f"[{ts}] {label}{str(msg)[:400]}\n"

        with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
            f.write(line)

        # Periodically trim so the file doesn't grow unbounded
        _maybe_trim()
    except Exception:
        pass  # Never let logging crash a background task


def _maybe_trim() -> None:
    """If the log exceeds _MAX_LINES, trim to _TRIM_TO keeping the newest lines."""
    try:
        text = ACTIVITY_LOG.read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) > _MAX_LINES:
            trimmed = "\n".join(lines[-_TRIM_TO:]) + "\n"
            ACTIVITY_LOG.write_text(trimmed, encoding="utf-8")
    except Exception:
        pass


def recent_lines(n: int = 100) -> list[str]:
    """Return the last n lines from the activity log."""
    try:
        if not ACTIVITY_LOG.exists():
            return []
        lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
        return lines[-n:] if len(lines) > n else lines
    except Exception:
        return []
