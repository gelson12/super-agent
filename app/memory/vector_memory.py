"""
Semantic cross-session memory using pgvector + Google text-embedding-004.

Every exchange is stored as a 768-dim vector embedding in PostgreSQL.
At dispatch time, the top-5 most semantically relevant past memories
are retrieved and injected into the message as context — so Super Agent
remembers facts, preferences, and patterns across sessions.

Falls back silently if:
  - DATABASE_URL is not set (SQLite has no pgvector support)
  - gemini_api_key is not set
  - pgvector extension not installed on the Postgres instance
"""
import json
import os

_enabled = False
_conn_str: str | None = None


def _get_conn_str() -> str | None:
    """Return a psycopg2-compatible connection string, or None if unavailable."""
    url = os.environ.get("DATABASE_URL", "")
    if not url or url.startswith("sqlite"):
        return None
    # Railway uses postgres:// — psycopg2 needs postgresql://
    return url.replace("postgres://", "postgresql://", 1)


def _init_table() -> bool:
    """Create the pgvector extension and memories table if they don't exist."""
    global _enabled, _conn_str
    _conn_str = _get_conn_str()
    if not _conn_str:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_memories (
                id SERIAL PRIMARY KEY,
                session_id TEXT,
                content TEXT,
                embedding vector(768),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS agent_memories_embedding_idx
                ON agent_memories USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100);
        """)
        cur.close()
        conn.close()
        _enabled = True
        return True
    except Exception:
        _enabled = False
        return False


# Attempt init at import time — silently skipped if unavailable
_init_table()


def _embed(text: str) -> list[float] | None:
    """Return 768-dim embedding via Google text-embedding-004, or None on error."""
    try:
        from google import genai as google_genai
        from ..config import settings
        if not settings.gemini_api_key:
            return None
        client = google_genai.Client(api_key=settings.gemini_api_key)
        result = client.models.embed_content(
            model="text-embedding-004",
            contents=text,
        )
        return result.embeddings[0].values
    except Exception:
        return None


def store_memory(session_id: str, content: str) -> None:
    """
    Store an exchange as a vector embedding. Best-effort — never raises.
    Called after every successful dispatch to build up the memory bank.
    """
    if not _enabled or not _conn_str:
        return
    try:
        embedding = _embed(content)
        if embedding is None:
            return
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_memories (session_id, content, embedding) VALUES (%s, %s, %s)",
            (session_id, content[:1000], json.dumps(embedding)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass


def retrieve_memories(query: str, top_k: int = 5) -> list[str]:
    """
    Return the top-k most semantically similar past memories for a query.
    Returns empty list if memory is disabled or no results found.
    """
    if not _enabled or not _conn_str:
        return []
    try:
        embedding = _embed(query)
        if embedding is None:
            return []
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT content FROM agent_memories
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (json.dumps(embedding), top_k),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


def get_memory_context(query: str) -> str:
    """
    Called at the start of every dispatch. Returns a formatted memory
    injection string, or empty string if no relevant memories exist.
    """
    memories = retrieve_memories(query)
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"[Relevant context from past sessions]\n{lines}\n\n"
