"""
Conversation memory — PostgreSQL-first, SQLite fallback.

Priority:
  1. PostgreSQL — when DATABASE_URL is set (Railway PostgreSQL plugin).
     All sessions persist forever, survive any container restart, and
     support concurrent workers without file locking.
  2. SQLite on Railway Persistent Volume (/workspace/agent_memory.db) —
     when DATABASE_URL is not set but /workspace is writable.
  3. SQLite in current directory — local/dev fallback.

Also provides get_compressed_context() which returns the last 6 messages
verbatim plus a Haiku-generated bullet-point summary of older messages,
keeping token usage bounded on long sessions.
"""
import logging
import os
import threading
import time as _time
from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain_core.messages import BaseMessage

_log = logging.getLogger("session")

# ── Compression cache ─────────────────────────────────────────────────────────
# Avoids calling Haiku on every request for long sessions.
# Key: session_id → (summary_text, epoch_ts, msg_count_at_compression)
_compress_cache: dict[str, tuple[str, float, int]] = {}
_compress_cache_lock = threading.Lock()
_COMPRESS_TTL = 1800  # 30 minutes


def _resolve_db_path() -> str:
    """
    Return the correct database connection string.

    Railway injects DATABASE_URL as "postgres://..." — SQLAlchemy requires
    "postgresql://..." so we normalise the prefix.
    """
    raw = os.environ.get("DATABASE_URL", "")
    if raw:
        # Normalise Railway's postgres:// → postgresql:// for SQLAlchemy
        return raw.replace("postgres://", "postgresql://", 1)
    # No PostgreSQL — use SQLite on persistent volume or local fallback
    db_dir = "/workspace" if os.access("/workspace", os.W_OK) else "."
    return f"sqlite:///{db_dir}/agent_memory.db"


DB_PATH = _resolve_db_path()


def get_session_history(session_id: str) -> SQLChatMessageHistory:
    """Return (or create) a message history store for a given session ID."""
    return SQLChatMessageHistory(
        session_id=session_id,
        connection=DB_PATH,
    )


def _cache_invalidate(session_id: str) -> None:
    with _compress_cache_lock:
        _compress_cache.pop(session_id, None)


def clear_session(session_id: str) -> None:
    """Wipe all messages for a given session."""
    history = get_session_history(session_id)
    history.clear()
    _cache_invalidate(session_id)


def append_exchange(session_id: str, user_msg: str, ai_msg: str) -> None:
    """Save one human→AI exchange to the session store."""
    try:
        history = get_session_history(session_id)
        history.add_user_message(user_msg)
        history.add_ai_message(ai_msg)
        # Invalidate compression cache — new messages make the cached summary stale
        _cache_invalidate(session_id)
    except Exception as e:
        _log.warning("append_exchange failed for session %s: %s", session_id, e)


def get_messages(session_id: str) -> list[BaseMessage]:
    """Return all messages for a session."""
    try:
        return get_session_history(session_id).messages
    except Exception as e:
        _log.warning("get_messages failed for session %s: %s", session_id, e)
        return []


def get_compressed_context(session_id: str) -> str:
    """
    Return a token-efficient context string for the current session.

    Strategy:
    - If <= 6 messages: return them verbatim as a formatted string.
    - If > 6 messages: use Haiku to summarise the older messages into
      3–5 bullet points, then append the 6 most recent messages verbatim.

    This bounds context token cost on long sessions while preserving
    recent conversational continuity.
    """
    messages = get_messages(session_id)
    if not messages:
        return ""

    def _format(msgs: list[BaseMessage]) -> str:
        return "\n".join(f"{m.type.upper()}: {m.content}" for m in msgs)

    if len(messages) <= 6:
        return _format(messages)

    # Summarise the older portion via Haiku
    old_msgs = messages[:-6]
    recent_msgs = messages[-6:]
    history_text = _format(old_msgs)

    # ── Compression cache check ───────────────────────────────────────────────
    # Reuse a cached summary if it's less than 30 min old AND message count
    # hasn't grown (i.e. no new messages have been pushed into old_msgs).
    with _compress_cache_lock:
        _cached = _compress_cache.get(session_id)
    if _cached is not None:
        _cached_summary, _cached_ts, _cached_count = _cached
        if (
            _time.time() - _cached_ts < _COMPRESS_TTL
            and _cached_count == len(old_msgs)
        ):
            summary = _cached_summary
            # Cap summary at 2000 chars to prevent unbounded context injection
            if len(summary) > 2000:
                summary = summary[:2000] + "\n[...summary truncated...]"
            recent_text = _format(recent_msgs)
            return f"[Summary of earlier conversation]\n{summary}\n\n[Recent messages]\n{recent_text}"

    try:
        from ..learning.internal_llm import ask_internal_fast
        from ..prompts import COMPRESSION_PROMPT
        summary_prompt = COMPRESSION_PROMPT.format(history=history_text)
        summary = ask_internal_fast(summary_prompt)
        # Cache the successful result
        with _compress_cache_lock:
            _compress_cache[session_id] = (summary, _time.time(), len(old_msgs))
    except Exception as e:
        _log.warning("Haiku compression failed for session: %s", e)
        # Fallback: keep the last 5 old messages so critical context isn't lost
        # (was 3 — too few when the key decision is 4–6 messages back)
        fallback_old = old_msgs[-5:] if len(old_msgs) > 5 else old_msgs
        summary = "[Earlier context — recent excerpt]\n" + _format(fallback_old)

    # Cap summary at 2000 chars to prevent unbounded context injection
    if len(summary) > 2000:
        summary = summary[:2000] + "\n[...summary truncated...]"

    recent_text = _format(recent_msgs)
    return f"[Summary of earlier conversation]\n{summary}\n\n[Recent messages]\n{recent_text}"
