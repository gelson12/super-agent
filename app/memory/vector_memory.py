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
    # Read API key directly from env so this works from any context
    # (cli_worker, app, external script) without relative import issues.
    try:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            try:
                from ..config import settings
                api_key = settings.gemini_api_key or ""
            except Exception:
                pass
        if not api_key:
            return None
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
        result = client.models.embed_content(model="text-embedding-004", contents=text)
        return result.embeddings[0].values
    except Exception:
        return None


def _ensure_source_columns() -> None:
    """
    Idempotent: add source, memory_type, importance, content_hash columns and
    performance indexes to agent_memories if they don't exist yet.
    Called once at module init when pg is available.
    """
    if not _pg_enabled or not _conn_str:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        conn.autocommit = True
        cur = conn.cursor()
        # Add columns
        for col, defn in [
            ("source",       "VARCHAR(64)  DEFAULT 'unknown'"),
            ("memory_type",  "VARCHAR(32)  DEFAULT 'general'"),
            ("importance",   "SMALLINT     DEFAULT 3"),
            ("tags",         "TEXT[]       DEFAULT '{}'"),
            ("content_hash", "VARCHAR(64)  DEFAULT NULL"),
        ]:
            cur.execute(f"""
                ALTER TABLE agent_memories
                ADD COLUMN IF NOT EXISTS {col} {defn};
            """)
        # Unique index on content_hash for dedup (skips NULL rows automatically)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS agent_memories_content_hash_idx
                ON agent_memories (content_hash)
                WHERE content_hash IS NOT NULL;
        """)
        # One-time backfill: compute content_hash for legacy rows that have NULL.
        # Done in a single UPDATE so subsequent deploys are instant no-ops (WHERE IS NULL).
        cur.execute("""
            UPDATE agent_memories
            SET content_hash = substr(md5(left(content, 500)), 1, 32)
            WHERE content_hash IS NULL
              AND content IS NOT NULL;
        """)
        # Indexes for session filtering and sorting
        cur.execute("""
            CREATE INDEX IF NOT EXISTS agent_memories_session_id_idx
                ON agent_memories (session_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS agent_memories_importance_idx
                ON agent_memories (importance DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS agent_memories_created_at_idx
                ON agent_memories (created_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS agent_memories_type_importance_idx
                ON agent_memories (memory_type, importance DESC);
        """)
        cur.close()
        conn.close()
    except Exception:
        pass


def upgrade_memory_importance(content_prefix: str, delta: int = 1) -> bool:
    """
    Upgrade the importance of an existing memory by `delta` (capped at 5).
    Matches on the first 100 chars of content. Used by the feedback loop to
    reinforce memories that were cited in highly-rated responses.
    Returns True if a row was updated.
    """
    if not _pg_enabled or not _conn_str:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            UPDATE agent_memories
            SET importance = LEAST(importance + %s, 5)
            WHERE LEFT(content, 100) = LEFT(%s, 100)
        """, (delta, content_prefix))
        updated = cur.rowcount > 0
        cur.close()
        conn.close()
        return updated
    except Exception:
        return False


def _evict_old_memories() -> int:
    """
    Delete low-importance memories older than 60 days to keep the table lean.
    Returns count deleted (0 on failure or if pg unavailable).
    Safe to call periodically — never raises.
    """
    if not _pg_enabled or not _conn_str:
        return 0
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM agent_memories
            WHERE importance <= 2
              AND created_at < NOW() - INTERVAL '60 days'
        """)
        deleted = cur.rowcount
        cur.close()
        conn.close()
        return deleted
    except Exception:
        return 0


_ensure_source_columns()


def _content_hash(content: str) -> str:
    """SHA-256 of the first 500 chars — used for dedup."""
    import hashlib
    return hashlib.sha256(content[:500].encode("utf-8")).hexdigest()[:64]


def _pg_store(session_id: str, content: str,
              source: str = "unknown",
              memory_type: str = "general",
              importance: int = 3) -> bool:
    try:
        embedding = _embed(content)  # None is acceptable — stored as NULL
        chash = _content_hash(content)
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        if embedding is not None:
            cur.execute(
                """INSERT INTO agent_memories
                   (session_id, content, embedding, source, memory_type, importance, content_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (content_hash) DO NOTHING""",
                (session_id, content[:1000], json.dumps(embedding),
                 source[:64], memory_type[:32], importance, chash),
            )
        else:
            # Store without embedding — text search still works, vector search skips these
            cur.execute(
                """INSERT INTO agent_memories
                   (session_id, content, source, memory_type, importance, content_hash)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (content_hash) DO NOTHING""",
                (session_id, content[:1000],
                 source[:64], memory_type[:32], importance, chash),
            )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        return False


# Minimum cosine similarity for a memory to be injected into context.
# Below this the memory is considered too unrelated and is dropped.
# Hit/miss counts are logged to _RETRIEVAL_STATS_PATH for weekly tuning.
_SIMILARITY_THRESHOLD = 0.72


def _retrieval_stats_path() -> str:
    if os.access("/workspace", os.W_OK):
        return "/workspace/memory_retrieval_stats.jsonl"
    return "./memory_retrieval_stats.jsonl"


def _log_retrieval_stats(hits: int, misses: int, session_id: str | None) -> None:
    """Append one retrieval batch summary to the stats log. Never raises."""
    try:
        record = {
            "ts": round(time.time(), 2),
            "hits": hits,
            "misses": misses,
            "threshold": _SIMILARITY_THRESHOLD,
            "session": session_id or "default",
        }
        with open(_retrieval_stats_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _pg_retrieve(query: str, top_k: int = 5, session_id: str | None = None) -> list[str]:
    """
    Retrieve memories ranked by combined vector similarity × importance score.
    Formula: distance / (1.0 + importance * 0.3) — stronger boost than the old
    divisor so importance=5 memories surface 2.5× ahead of importance=1 ones.
      importance=5 → divisor 2.5  (strong boost)
      importance=3 → divisor 1.9  (default)
      importance=1 → divisor 1.3  (slight penalty)

    Rows with cosine similarity below _SIMILARITY_THRESHOLD are dropped before
    being returned — the hit/miss counts are appended to the retrieval stats log
    so the weekly review can tune the threshold.

    If session_id is provided, only returns memories for that session.
    """
    try:
        embedding = _embed(query)
        if embedding is None:
            return []
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        # Over-fetch so threshold filtering still yields up to top_k results.
        fetch_k = max(top_k * 3, top_k + 5)
        if session_id:
            cur.execute(
                """SELECT content, (embedding <=> %s::vector) AS distance
                   FROM agent_memories
                   WHERE session_id = %s AND embedding IS NOT NULL
                   ORDER BY (embedding <=> %s::vector) / (1.0 + COALESCE(importance, 3) * 0.3)
                   LIMIT %s""",
                (json.dumps(embedding), session_id, json.dumps(embedding), fetch_k),
            )
        else:
            cur.execute(
                """SELECT content, (embedding <=> %s::vector) AS distance
                   FROM agent_memories
                   WHERE embedding IS NOT NULL
                   ORDER BY (embedding <=> %s::vector) / (1.0 + COALESCE(importance, 3) * 0.3)
                   LIMIT %s""",
                (json.dumps(embedding), json.dumps(embedding), fetch_k),
            )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        # pgvector cosine distance = 1 − cosine_similarity
        hits, misses, results = 0, 0, []
        for content, distance in rows:
            similarity = 1.0 - float(distance) if distance is not None else 0.0
            if similarity >= _SIMILARITY_THRESHOLD:
                results.append(content)
                hits += 1
                if len(results) >= top_k:
                    break
            else:
                misses += 1
        _log_retrieval_stats(hits, misses, session_id)
        return results
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


# In-memory prefix set for O(1) JSON dedup — populated lazily on first store call.
# Keyed by the path string so it resets if path changes (Railway /workspace vs local).
_json_prefix_cache: dict[str, set[str]] = {}   # path_str → set of content[:60]
_json_prefix_loaded: dict[str, bool]   = {}    # path_str → whether file was scanned


def _ensure_prefix_cache(path: Path) -> set[str]:
    """
    Lazily load content prefixes from the JSONL file into an in-memory set.
    Only scans the file once per process lifetime per path.
    Returns the live set (callers mutate it directly).
    """
    key = str(path)
    if not _json_prefix_loaded.get(key):
        prefixes: set[str] = set()
        if path.exists():
            try:
                with path.open(encoding="utf-8") as _f:
                    for _line in _f:
                        try:
                            _rec = json.loads(_line.strip())
                            c = _rec.get("content", "")
                            if c:
                                prefixes.add(c[:60])
                        except Exception:
                            pass
            except Exception:
                pass
        _json_prefix_cache[key] = prefixes
        _json_prefix_loaded[key] = True
    return _json_prefix_cache[key]


def _json_store(session_id: str, content: str) -> None:
    """
    Append one memory record to the JSONL file.
    Embeds ISO timestamp + auto-extracted topics directly into the stored content
    so that when injected into a prompt the model sees WHEN and WHAT the memory is about.
    Dedup via in-memory prefix set — O(1) after first call.
    """
    try:
        path = _json_path()
        prefix = content[:60]
        prefixes = _ensure_prefix_cache(path)
        if prefix in prefixes:
            return  # duplicate — skip
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
        # Update in-memory set so next store in this process doesn't need a file scan
        prefixes.add(tagged[:60])
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


def _get_pg_conn():
    """Return a new psycopg2 connection or None if unavailable."""
    if not _pg_enabled or not _conn_str:
        return None
    try:
        import psycopg2
        return psycopg2.connect(_conn_str)
    except Exception:
        return None


def search_memories(query: str, limit: int = 20, min_importance: int = 1,
                    source: str | None = None) -> list[dict]:
    """
    Keyword search across the memory store. Returns structured dicts.
    Falls back to full scan with ILIKE when vector search unavailable.
    Used by /memory/search endpoint so agents can explicitly query the KB.
    """
    results = []
    if _pg_enabled and _conn_str:
        try:
            import psycopg2
            conn = psycopg2.connect(_conn_str)
            cur = conn.cursor()
            source_clause = "AND source = %s" if source else ""
            params = [f"%{query}%", min_importance]
            if source:
                params.append(source)
            params.append(limit)
            cur.execute(f"""
                SELECT content, source, memory_type, importance, created_at
                FROM agent_memories
                WHERE content ILIKE %s
                  AND importance >= %s
                  {source_clause}
                ORDER BY importance DESC, created_at DESC
                LIMIT %s
            """, params)
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
        except Exception:
            pass
    return results


def prune_old_memories(days: int = 90, max_importance: int = 2) -> int:
    """
    Delete memories older than `days` with importance <= max_importance.
    Keeps high-importance memories indefinitely. Returns number deleted.
    Run weekly to prevent unbounded growth.
    """
    if not _pg_enabled or not _conn_str:
        return 0
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM agent_memories
            WHERE created_at < NOW() - INTERVAL '%s days'
              AND importance <= %s
        """, (days, max_importance))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception:
        return 0


def deduplicate_memories(similarity_threshold: int = 90) -> int:
    """
    Remove exact and near-duplicate memories. Keeps the highest-importance
    version when duplicates exist (same content_hash or content prefix match).
    Returns number of duplicates removed.
    """
    if not _pg_enabled or not _conn_str:
        return 0
    removed = 0
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        # Remove exact duplicates keeping highest importance
        cur.execute("""
            DELETE FROM agent_memories
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY LEFT(content, 200)
                               ORDER BY importance DESC, created_at DESC
                           ) AS rn
                    FROM agent_memories
                ) ranked
                WHERE rn > 1
            )
        """)
        removed = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass
    return removed


def memory_health_report() -> dict:
    """
    Return a detailed health report of the memory store.
    Used by the weekly performance report job.
    """
    if not _pg_enabled or not _conn_str:
        return {"error": "PostgreSQL unavailable"}
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM agent_memories")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM agent_memories WHERE embedding IS NOT NULL")
        with_embedding = cur.fetchone()[0]
        cur.execute("""
            SELECT source, COUNT(*), AVG(importance)::numeric(4,2)
            FROM agent_memories GROUP BY source ORDER BY COUNT(*) DESC
        """)
        by_source = [{"source": r[0], "count": r[1], "avg_importance": float(r[2] or 0)}
                     for r in cur.fetchall()]
        cur.execute("""
            SELECT COUNT(*) FROM agent_memories
            WHERE created_at > NOW() - INTERVAL '7 days'
        """)
        last_7d = cur.fetchone()[0]
        cur.execute("""
            SELECT COUNT(*) FROM agent_memories
            WHERE created_at > NOW() - INTERVAL '24 hours'
        """)
        last_24h = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {
            "total": total,
            "with_embedding": with_embedding,
            "without_embedding": total - with_embedding,
            "embedding_coverage_pct": round(with_embedding / total * 100, 1) if total else 0,
            "last_24h": last_24h,
            "last_7d": last_7d,
            "by_source": by_source,
        }
    except Exception as e:
        return {"error": str(e)}


def retrieve_memories(query: str, top_k: int = 8,
                      session_id: str | None = None) -> list[str]:
    """
    Return the top-k most relevant past memories for a query.
    If session_id is provided, pg retrieval is scoped to that session only.
    Uses pgvector if available, keyword scoring otherwise.
    """
    if _pg_enabled and _conn_str:
        results = _pg_retrieve(query, top_k, session_id=session_id)
        if results:
            return results
    return _json_retrieve(query, top_k)


def _extract_ts_prefix(m: str) -> str:
    """Extract [YYYY-MM-DD HH:MM UTC] prefix if present, return as '(DATE) '."""
    import re as _re
    match = _re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)\]', m)
    return f"({match.group(1)}) " if match else ""


def get_memory_context(query: str, top_k: int = 8,
                       session_id: str | None = None) -> str:
    """
    Called at the start of every dispatch.
    Returns a formatted context block of relevant past memories including timestamps,
    or empty string. top_k=8 ensures richer cross-session recall.
    Pass session_id to scope retrieval to the current session only.
    """
    memories = retrieve_memories(query, top_k=top_k, session_id=session_id)
    if not memories:
        return ""

    # Extract timestamps for header (oldest/newest range)
    import re as _re
    _ts_pattern = _re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)\]')
    timestamps = [m.group(1) for mem in memories for m in [_ts_pattern.search(mem)] if m]
    if timestamps:
        header = (
            f"[Cross-session memory — {len(memories)} memories, "
            f"oldest: {min(timestamps)}, newest: {max(timestamps)}]"
        )
    else:
        header = f"[Cross-session memory — {len(memories)} memories]"

    lines = "\n".join(f"- {_extract_ts_prefix(m)}{m}" for m in memories)
    return (
        f"{header}\n"
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


def backfill_embeddings(batch_size: int = 20) -> dict:
    """
    Nightly job: pick agent_memories rows where embedding IS NULL and generate embeddings.
    Processes up to batch_size rows per call to respect Gemini rate limits.
    Ordered by importance DESC so high-priority memories get vectors first.
    Returns {"processed": N, "success": M, "skipped": K}.
    """
    if not _pg_enabled or not _conn_str:
        return {"processed": 0, "success": 0, "skipped": 0, "error": "pg unavailable"}
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, content FROM agent_memories
            WHERE embedding IS NULL
            ORDER BY importance DESC, created_at DESC
            LIMIT %s
        """, (batch_size,))
        rows = cur.fetchall()
        success = 0
        skipped = 0
        for row_id, content in rows:
            emb = _embed(content)
            if emb is None:
                skipped += 1
                continue
            cur.execute(
                "UPDATE agent_memories SET embedding = %s WHERE id = %s",
                (json.dumps(emb), row_id),
            )
            success += 1
        conn.commit()
        cur.close()
        conn.close()
        return {"processed": len(rows), "success": success, "skipped": skipped}
    except Exception as e:
        return {"processed": 0, "success": 0, "skipped": 0, "error": str(e)}


def apply_importance_decay() -> int:
    """
    Apply time-based importance decay to memories that become stale:
    - 'fact', 'goal', 'general' memories older than 30 days lose 1 importance point
    - 'preference' and 'decision' memories NEVER decay (user choices are persistent)
    - Importance floor is 1 (memories are never deleted by decay alone)
    Returns number of rows updated.
    """
    if not _pg_enabled or not _conn_str:
        return 0
    try:
        import psycopg2
        conn = psycopg2.connect(_conn_str)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            UPDATE agent_memories
            SET importance = GREATEST(importance - 1, 1)
            WHERE memory_type IN ('fact', 'goal', 'general')
              AND importance > 1
              AND created_at < NOW() - INTERVAL '30 days'
        """)
        updated = cur.rowcount
        cur.close()
        conn.close()
        return updated
    except Exception:
        return 0
