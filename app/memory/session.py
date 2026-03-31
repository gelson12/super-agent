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
import os
from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain_core.messages import BaseMessage


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


def clear_session(session_id: str) -> None:
    """Wipe all messages for a given session."""
    history = get_session_history(session_id)
    history.clear()


def append_exchange(session_id: str, user_msg: str, ai_msg: str) -> None:
    """Save one human→AI exchange to the session store."""
    history = get_session_history(session_id)
    history.add_user_message(user_msg)
    history.add_ai_message(ai_msg)


def get_messages(session_id: str) -> list[BaseMessage]:
    """Return all messages for a session."""
    return get_session_history(session_id).messages


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

    try:
        from ..models.claude import ask_claude_haiku
        from ..prompts import COMPRESSION_PROMPT
        summary_prompt = COMPRESSION_PROMPT.format(history=history_text)
        summary = ask_claude_haiku(summary_prompt)
    except Exception:
        # If summarisation fails, fall back to last 6 only
        summary = "[Earlier context unavailable]"

    recent_text = _format(recent_msgs)
    return f"[Summary of earlier conversation]\n{summary}\n\n[Recent messages]\n{recent_text}"
