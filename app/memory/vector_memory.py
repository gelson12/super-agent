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


def _ensure_source_columns() -> None:
    """
    Idempotent: add source, memory_type, importance columns to agent_memories
    if they don't exist yet. Called once at module init when pg is available.
    These enable attribution (which model/source wrote this) and priority ranking.
    """
    if not _pg_enabled or not _conn_str:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        conn.autocommit = True
        cur = conn.cursor()
        for col, defn in [
            ("source",      "VARCHAR(64)  DEFAULT 'unknown'"),
            ("memory_type", "VARCHAR(32)  DEFAULT 'general'"),
            ("importance",  "SMALLINT     DEFAULT 3"),
            ("tags",        "TEXT[]       DEFAULT '{}'"),
        ]:
            cur.execute(f"""
                ALTER TABLE agent_memories
                ADD COLUMN IF NOT EXISTS {col} {defn};
            """)
        cur.close()
        conn.close()
    except Exception:
        pass


_ensure_source_columns()


def _pg_store(session_id: str, content: str,
              source: str = "unknown",
              memory_type: str = "general",
              importance: int = 3) -> bool:
    try:
        embedding = _embed(content)
        if embedding is None:
            return False
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO agent_memories
               (session_id, content, embedding, source, memory_type, importance)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (session_id, content[:1000], json.dumps(embedding),
             source[:64], memory_type[:32], importance),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        return False


def _pg_retrieve(query: str, top_k: int = 5) -> list[str]:
    """
    Retrieve memories ranked by combined vector similarity × importance score.
    Formula divides distance by (0.7 + importance*0.06) so higher-importance
    memories surface first over pure cosine-similarity ordering. This prevents
    high-volume low-importance memories from burying critical decisions.
      importance=5 → divisor 1.0  (full boost)
      importance=3 → divisor 0.88 (default)
      importance=1 → divisor 0.76 (slight penalty)
    """
    try:
        embedding = _embed(query)
        if embedding is None:
            return []
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute(
            """SELECT content
               FROM agent_memories
               ORDER BY (embedding <=> %s::vector) / (0.7 + COALESCE(importance, 3) * 0.06)
               LIMIT %s""",
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
                # Boost enriched memories by importance level
                content = rec["content"]
                if content.startswith("[IMPORTANT:"):
                    try:
                        _tag_end = content.index("]")
                        _parts = content[1:_tag_end].split(":")
                        _imp = int(_parts[2]) if len(_parts) >= 3 else 3
                        score *= 1.0 + (_imp * 0.15)  # +15% per importance level
                    except (ValueError, IndexError):
                        score *= 1.3  # default 30% boost
                scored.append((score, content))

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

def store_memory(session_id: str, content: str,
                 source: str = "unknown") -> None:
    """
    Store an exchange in long-term memory.
    Uses pgvector if available, JSON file otherwise.
    Best-effort — never raises.

    source: which agent/model wrote this memory
      "super_agent"   — API call via dispatcher
      "claude_code"   — synced from Claude Code local memory files
      "cli_pro"       — inspiring-cat Claude CLI Pro task result
      "auto_extract"  — auto-extracted insight (Haiku distillation)
      "unknown"       — legacy, unattributed
    """
    if _pg_enabled and _conn_str:
        stored = _pg_store(session_id, content, source=source)
        if stored:
            return
    # Always write to JSON fallback — acts as secondary backup even when pg works
    _json_store(session_id, content)


def store_enriched_memory(
    session_id: str,
    content: str,
    memory_type: str = "general",
    importance: int = 3,
    source: str = "unknown",
) -> None:
    """
    Store a memory with enriched metadata for proactive recall.

    memory_type: one of "decision", "preference", "fact", "goal", "problem"
    importance: 1 (low) to 5 (critical)
    source: which agent/process wrote this (see store_memory docstring)

    High-importance memories are tagged so retrieval can boost them.
    """
    tagged = f"[IMPORTANT:{memory_type}:{importance}] {content}"
    if _pg_enabled and _conn_str:
        stored = _pg_store(session_id, tagged,
                           source=source,
                           memory_type=memory_type,
                           importance=importance)
        if stored:
            _json_store(session_id, tagged)  # always write JSON too
            return
    _json_store(session_id, tagged)


def ingest_external_memory(
    content: str,
    memory_type: str = "fact",
    importance: int = 3,
    source: str = "claude_code",
    session_id: str = "shared",
) -> bool:
    """
    Store a memory that originated OUTSIDE the current session —
    e.g. synced from Claude Code local markdown files, or from the
    inspiring-cat CLI Pro container.

    This is the write path for the unified cross-model memory system.
    Returns True if stored successfully.
    """
    try:
        tagged = f"[IMPORTANT:{memory_type}:{importance}][source:{source}] {content[:800]}"
        if _pg_enabled and _conn_str:
            return _pg_store(session_id, tagged,
                             source=source,
                             memory_type=memory_type,
                             importance=importance)
        _json_store(session_id, tagged)
        return True
    except Exception:
        return False


def export_memories(limit: int = 100, min_importance: int = 3) -> list[dict]:
    """
    Export recent important memories as structured dicts.
    Used by the /memory/export endpoint so Claude Code can pull
    cross-session insights and write them to local memory files.
    """
    results = []
    if _pg_enabled and _conn_str:
        try:
            import psycopg2
            conn = psycopg2.connect(_conn_str)
            cur = conn.cursor()
            cur.execute("""
                SELECT content, source, memory_type, importance, created_at
                FROM agent_memories
                WHERE importance >= %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (min_importance, limit))
            for row in cur.fetchall():
                results.append({
                    "content": row[0],
                    "source": row[1] or "unknown",
                    "memory_type": row[2] or "general",
                    "importance": row[3] or 3,
                    "created_at": row[4].isoformat() if row[4] else None,
                })
            cur.close()
            conn.close()
            return results
        except Exception:
            pass
    # JSON fallback
    try:
        path = _json_path()
        if not path.exists():
            return []
        records = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line.strip()))
                except Exception:
                    pass
        records = [r for r in records if "[IMPORTANT:" in r.get("content", "")]
        records.sort(key=lambda r: r.get("ts", 0), reverse=True)
        for r in records[:limit]:
            results.append({
                "content": r["content"],
                "source": "json_fallback",
                "memory_type": "general",
                "importance": 3,
                "created_at": None,
            })
    except Exception:
        pass
    return results


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


# ── Auto-insight extraction ────────────────────────────────────────────────────
# Runs in a daemon thread after every significant exchange. Uses Claude Haiku
# to distil 1-3 key facts/decisions/preferences from the Q&A pair and stores
# them as enriched memories. This is the "accumulate and get smarter" mechanism:
# raw Q&A → distilled knowledge that survives context window limits.

_extract_lock = __import__("threading").Lock()
_extract_cooldown: dict = {}   # session_id → last_extract_epoch
_EXTRACT_INTERVAL = 300        # at most one extraction per 5 min per session
_MIN_RESPONSE_LEN_FOR_EXTRACT = 300   # only worthwhile on substantive answers


def extract_and_store_insights(
    message: str,
    response: str,
    model: str,
    session_id: str,
    source: str = "auto_extract",
) -> None:
    """
    Fire-and-forget: distil the exchange into 1-3 named insights.
    Runs in a daemon thread — never blocks the response path.
    Never raises.
    """
    if len(response) < _MIN_RESPONSE_LEN_FOR_EXTRACT:
        return

    import threading as _thr

    def _run():
        try:
            import time as _time
            # Per-session cooldown — avoid flooding on rapid-fire short exchanges
            with _extract_lock:
                last = _extract_cooldown.get(session_id, 0)
                if _time.time() - last < _EXTRACT_INTERVAL:
                    return
                _extract_cooldown[session_id] = _time.time()

            from ..models.claude import ask_claude_haiku as _haiku
            prompt = (
                "Extract 1-3 concise, reusable facts, decisions, or preferences "
                "from this conversation exchange. Each fact must be a single sentence, "
                "self-contained (no pronouns referring to the exchange), and useful in "
                "future conversations. Return ONLY a JSON array of strings, no commentary.\n\n"
                f"User: {message[:500]}\n\nAgent: {response[:800]}"
            )
            raw = _haiku(prompt, system="You are a memory distillation engine. Output only valid JSON.")
            raw = raw.strip()
            # parse the JSON array
            import json as _json
            # strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            facts = _json.loads(raw)
            if not isinstance(facts, list):
                return
            for fact in facts[:3]:
                if isinstance(fact, str) and len(fact) > 20:
                    ingest_external_memory(
                        content=fact,
                        memory_type="fact",
                        importance=3,
                        source=f"{source}:{model}",
                        session_id=session_id,
                    )
        except Exception:
            pass  # Never let extraction fail loudly

    t = _thr.Thread(target=_run, daemon=True)
    t.start()
