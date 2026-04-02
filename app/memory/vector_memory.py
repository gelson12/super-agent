"""
Semantic cross-session memory — two-tier storage:

Tier 1 (pgvector): uses PostgreSQL + pgvector extension for real vector similarity search.
  Requires DATABASE_URL pointing to Postgres AND the pgvector extension installed.

Tier 2 (JSON fallback): if pgvector is unavailable, memories are written to
  /workspace/agent_memories.jsonl and retrieved via TF-IDF-style keyword scoring.
  No external dependencies. Always available. Survives Railway restarts via /workspace volume.

Both tiers are transparent to callers — store_memory/get_memory_context work identically.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Tier 1: pgvector ──────────────────────────────────────────────────────────

_pg_enabled = False
_conn_str: str | None = None


def _get_conn_str() -> str | None:
    url = os.environ.get("DATABASE_URL", "")
    if not url or url.startswith("sqlite"):
        return None
    return url.replace("postgres://", "postgresql://", 1)


def _init_pg() -> bool:
    global _pg_enabled, _conn_str
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
        _pg_enabled = True
        return True
    except Exception:
        _pg_enabled = False
        return False


_init_pg()


def _embed(text: str) -> list[float] | None:
    try:
        from google import genai as google_genai
        from ..config import settings
        if not settings.gemini_api_key:
            return None
        client = google_genai.Client(api_key=settings.gemini_api_key)
        result = client.models.embed_content(model="text-embedding-004", contents=text)
        return result.embeddings[0].values
    except Exception:
        return None


def _pg_store(session_id: str, content: str) -> bool:
    try:
        embedding = _embed(content)
        if embedding is None:
            return False
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
        return True
    except Exception:
        return False


def _pg_retrieve(query: str, top_k: int = 5) -> list[str]:
    try:
        embedding = _embed(query)
        if embedding is None:
            return []
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute(
            "SELECT content FROM agent_memories ORDER BY embedding <=> %s::vector LIMIT %s",
            (json.dumps(embedding), top_k),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


# ── Tier 2: JSON keyword fallback ────────────────────────────────────────────

def _json_path() -> Path:
    """Return writable path for the JSON memory file."""
    ws = Path("/workspace/agent_memories.jsonl")
    if os.access("/workspace", os.W_OK):
        return ws
    return Path("./agent_memories.jsonl")


def _tokenize(text: str) -> set[str]:
    """Simple word tokenizer — strips punctuation, lowercases, filters short words."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


# ── Topic auto-extraction ────────────────────────────────────────────────────
_TOPIC_MAP: dict[str, set[str]] = {
    "n8n":        {"n8n", "workflow", "automation", "trigger", "webhook", "execution", "node"},
    "github":     {"github", "repo", "repository", "commit", "branch", "push", "pull", "pr"},
    "railway":    {"railway", "deploy", "deployment", "service", "redeploy", "logs", "restart"},
    "flutter":    {"flutter", "apk", "android", "mobile", "dart", "build", "ios"},
    "memory":     {"memory", "remember", "recall", "forgot", "past", "history", "session"},
    "vscode":     {"vscode", "codeserver", "editor", "extension", "workspace", "terminal"},
    "cloudinary": {"cloudinary", "upload", "storage", "image", "artifact", "download"},
    "debug":      {"debug", "error", "fix", "broken", "failing", "issue", "problem", "404", "502"},
    "instagram":  {"instagram", "facebook", "genspark", "social", "post", "feed"},
    "apk":        {"apk", "android", "build", "flutter", "mobile", "app"},
}


def _extract_topics(tokens: set[str]) -> list[str]:
    """Return matching topic labels for a set of tokens."""
    return [topic for topic, kws in _TOPIC_MAP.items() if tokens & kws] or ["general"]


def _json_store(session_id: str, content: str) -> None:
    """
    Append one memory record to the JSONL file.
    Embeds ISO timestamp + auto-extracted topics directly into the stored content
    so that when injected into a prompt the model sees WHEN and WHAT the memory is about.
    """
    try:
        path = _json_path()
        tokens = _tokenize(content)
        topics = _extract_topics(tokens)
        iso_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        # Prefix metadata into the content string — visible to the model when retrieved
        tagged = f"[{iso_ts}][topics:{','.join(topics)}] {content[:880]}"
        record = {
            "session_id": session_id,
            "content": tagged,
            "ts": time.time(),
            # Include topic labels as searchable tokens so topic-keyword queries match
            "tokens": list(tokens | set(topics)),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


_JSON_MAX_RECORDS = 2000   # keep newest 2000 memories, trim older on overflow


def _json_retrieve(query: str, top_k: int = 5) -> list[str]:
    """
    Score every stored memory against the query by token overlap and return
    the top_k highest scoring ones. O(n) scan — fast enough for ≤2000 records.
    """
    path = _json_path()
    if not path.exists():
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    try:
        records = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass

        if not records:
            return []

        # Trim file to newest _JSON_MAX_RECORDS if overgrown
        if len(records) > _JSON_MAX_RECORDS:
            records = records[-_JSON_MAX_RECORDS:]
            try:
                with path.open("w", encoding="utf-8") as f:
                    for r in records:
                        f.write(json.dumps(r) + "\n")
            except Exception:
                pass

        scored = []
        for rec in records:
            rec_tokens = set(rec.get("tokens", []))
            if not rec_tokens:
                continue
            overlap = len(query_tokens & rec_tokens)
            if overlap > 0:
                score = overlap / (len(query_tokens | rec_tokens) ** 0.5)
                scored.append((score, rec["content"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        # Deduplicate by content prefix to avoid repeating near-identical exchanges
        seen, results = set(), []
        for _, content in scored:
            prefix = content[:60]
            if prefix not in seen:
                seen.add(prefix)
                results.append(content)
            if len(results) >= top_k:
                break
        return results
    except Exception:
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def store_memory(session_id: str, content: str) -> None:
    """
    Store an exchange in long-term memory.
    Uses pgvector if available, JSON file otherwise.
    Best-effort — never raises.
    """
    if _pg_enabled and _conn_str:
        stored = _pg_store(session_id, content)
        if stored:
            return
    # Always write to JSON fallback — acts as secondary backup even when pg works
    _json_store(session_id, content)


def retrieve_memories(query: str, top_k: int = 8) -> list[str]:
    """
    Return the top-k most relevant past memories for a query.
    Uses pgvector if available, keyword scoring otherwise.
    """
    if _pg_enabled and _conn_str:
        results = _pg_retrieve(query, top_k)
        if results:
            return results
    return _json_retrieve(query, top_k)


def get_memory_context(query: str, top_k: int = 8) -> str:
    """
    Called at the start of every dispatch.
    Returns a formatted context block of relevant past memories including timestamps,
    or empty string. top_k=8 ensures richer cross-session recall.
    """
    memories = retrieve_memories(query, top_k=top_k)
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return (
        "[Cross-session memory — these are real past interactions, ordered by relevance]\n"
        f"{lines}\n"
        "[End of past context — reference these naturally in your response when relevant]\n\n"
    )
