"""
SQLite-backed per-session conversation memory.
Uses LangChain's SQLChatMessageHistory for local persistence.
No external services required — data stored in agent_memory.db.
"""
from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain_core.messages import BaseMessage

DB_PATH = "sqlite:///agent_memory.db"


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
