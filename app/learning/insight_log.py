"""
Interaction logger — records every dispatch event.

Dual-write: PostgreSQL (primary, survives restarts) + JSON file (fallback).
All query methods prefer PostgreSQL when DATABASE_URL is set.
"""
import json
import os
import threading
import time
from typing import Optional


# ── PostgreSQL helpers ────────────────────────────────────────────────────────

_pg_enabled = False
_pg_lock    = threading.Lock()


def _pg_conn():
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        return None
    try:
        import psycopg2
        return psycopg2.connect(raw.replace("postgres://", "postgresql://", 1))
    except Exception:
        return None


def _ensure_pg_table() -> bool:
    """Create agent_insights table. Returns True when PG is usable."""
    global _pg_enabled
    conn = _pg_conn()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_insights (
                        id              SERIAL PRIMARY KEY,
                        ts              DOUBLE PRECISION NOT NULL,
                        msg_words       INTEGER,
                        model           TEXT,
                        routed_by       TEXT,
                        complexity      INTEGER,
                        resp_len        INTEGER,
                        error           BOOLEAN DEFAULT FALSE,
                        error_category  TEXT,
                        session_id      TEXT,
                        latency_ms      DOUBLE PRECISION,
                        confidence      DOUBLE PRECISION,
                        memory_hits     INTEGER DEFAULT 0,
                        cache_hit       BOOLEAN DEFAULT FALSE,
                        feedback_applied BOOLEAN DEFAULT FALSE,
                        message_prefix  TEXT,
                        rating          INTEGER
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ai_ts
                    ON agent_insights(ts DESC)
                """)
        with _pg_lock:
            _pg_enabled = True
        return True
    except Exception:
        return False
    finally:
        try: conn.close()
        except Exception: pass


def _pg_insert(entry: dict) -> None:
    """Background-thread insert — never raises."""
    conn = _pg_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO agent_insights
                        (ts, msg_words, model, routed_by, complexity, resp_len,
                         error, error_category, session_id, latency_ms, confidence,
                         memory_hits, cache_hit, message_prefix)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    entry.get("ts"),
                    entry.get("msg_words"),
                    entry.get("model"),
                    entry.get("routed_by"),
                    entry.get("complexity"),
                    entry.get("resp_len"),
                    entry.get("error", False),
                    entry.get("error_category"),
                    entry.get("session"),
                    entry.get("latency_ms"),
                    entry.get("confidence"),
                    entry.get("memory_hits", 0),
                    entry.get("cache_hit", False),
                    entry.get("message_prefix"),
                ))
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass


def _pg_load(hours: float = 24.0, limit: Optional[int] = None) -> list[dict]:
    """Read entries from PG. Returns [] on error."""
    conn = _pg_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            if limit:
                cur.execute("""
                    SELECT ts, msg_words, model, routed_by, complexity, resp_len,
                           error, error_category, session_id, latency_ms, confidence,
                           memory_hits, cache_hit, feedback_applied, rating
                    FROM agent_insights ORDER BY ts DESC LIMIT %s
                """, (limit,))
                rows = list(reversed(cur.fetchall()))
            else:
                cutoff = time.time() - hours * 3600
                cur.execute("""
                    SELECT ts, msg_words, model, routed_by, complexity, resp_len,
                           error, error_category, session_id, latency_ms, confidence,
                           memory_hits, cache_hit, feedback_applied, rating
                    FROM agent_insights WHERE ts >= %s ORDER BY ts ASC
                """, (cutoff,))
                rows = cur.fetchall()
        entries = []
        for r in rows:
            e: dict = {
                "ts": r[0], "msg_words": r[1], "model": r[2],
                "routed_by": r[3], "complexity": r[4], "resp_len": r[5],
                "error": r[6], "session": r[8],
            }
            if r[7]:  e["error_category"]   = r[7]
            if r[9]:  e["latency_ms"]        = r[9]
            if r[10]: e["confidence"]        = r[10]
            if r[11]: e["memory_hits"]       = r[11]
            if r[12]: e["cache_hit"]         = r[12]
            if r[13]: e["feedback_applied"]  = r[13]
            if r[14]: e["rating"]            = r[14]
            entries.append(e)
        return entries
    except Exception:
        return []
    finally:
        try: conn.close()
        except Exception: pass


# ── File fallback ─────────────────────────────────────────────────────────────

def _resolve_path() -> str:
    for candidate in ("/workspace/super_agent_insights.json", "./super_agent_insights.json"):
        directory = os.path.dirname(candidate) or "."
        if os.access(directory, os.W_OK):
            return candidate
    return "./super_agent_insights.json"


LOG_PATH = _resolve_path()


# ── Error categorisation ──────────────────────────────────────────────────────

def _categorize_error(response: str) -> str:
    lower = response.lower()
    if any(k in lower for k in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(k in lower for k in ("connection", "connect error", "unreachable", "refused")):
        return "network"
    if any(k in lower for k in ("401", "403", "unauthorized", "forbidden", "api key", "not set")):
        return "auth"
    if any(k in lower for k in ("404", "not found")):
        return "not_found"
    if any(k in lower for k in ("500", "502", "503", "server error", "internal error")):
        return "server_error"
    if any(k in lower for k in ("oom", "memory", "out of memory", "killed")):
        return "oom"
    if any(k in lower for k in ("circuit breaker", "circuit_breaker")):
        return "circuit_breaker"
    if any(k in lower for k in ("rate limit", "too many", "quota")):
        return "rate_limit"
    return "unknown"


def _normalize_model(raw: str) -> str:
    m = (raw or "UNKNOWN").upper()
    _MAP = {
        "CLAUDE+SEARCH": "CLAUDE", "SELF_IMPROVE": "CLAUDE",
        "SHELL": "CLAUDE", "GITHUB": "CLAUDE", "N8N": "CLAUDE",
        "GEMINI_CLI": "GEMINI",
    }
    return _MAP.get(m, m)


# ── InsightLog ────────────────────────────────────────────────────────────────

class InsightLog:
    def __init__(self) -> None:
        self._buffer: list[dict] = []
        self._total  = 0
        # Try to set up PG table once at import time (non-blocking)
        threading.Thread(target=_ensure_pg_table, daemon=True).start()

    # ── Write ─────────────────────────────────────────────────────────────────

    def record(
        self,
        message: str,
        model: str,
        response: str,
        routed_by: str,
        complexity: int,
        session: Optional[str] = None,
        latency_ms: Optional[float] = None,
        confidence: Optional[float] = None,
        memory_hits: int = 0,
        cache_hit: bool = False,
    ) -> None:
        entry: dict = {
            "ts":        round(time.time(), 2),
            "msg_words": len(message.split()),
            "model":     model,
            "routed_by": routed_by,
            "complexity": complexity,
            "resp_len":  len(response),
            "error":     response.startswith("[") and response.endswith("]"),
            "session":   session or "default",
            "message_prefix": message[:200],
        }
        if entry["error"]:
            entry["error_category"] = _categorize_error(response)
        if latency_ms  is not None: entry["latency_ms"]  = round(latency_ms, 1)
        if confidence  is not None: entry["confidence"]  = round(confidence, 3)
        if memory_hits:             entry["memory_hits"] = memory_hits
        if cache_hit:               entry["cache_hit"]   = True

        self._buffer.append(entry)
        self._total += 1

        # Write to PG in background (non-blocking)
        threading.Thread(target=_pg_insert, args=(entry,), daemon=True).start()

        if len(self._buffer) >= 3:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        existing: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.extend(self._buffer)
        try:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except OSError:
            pass
        self._buffer.clear()

    # ── Read ──────────────────────────────────────────────────────────────────

    def _load_all(self, hours: float = 720.0) -> list[dict]:
        """Return all entries: PG (preferred) or file + buffer."""
        with _pg_lock:
            pg_ok = _pg_enabled
        if pg_ok:
            return _pg_load(hours=hours) + self._buffer
        # File fallback
        on_disk: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
            except (json.JSONDecodeError, OSError):
                on_disk = []
        return on_disk + self._buffer

    def get_recent_entries(self, n: int = 50) -> list:
        with _pg_lock:
            pg_ok = _pg_enabled
        if pg_ok:
            return list(reversed(_pg_load(limit=n)))
        all_entries = self._load_all()
        return list(reversed(all_entries[-n:]))

    # ── Analytics ─────────────────────────────────────────────────────────────

    # Hard floor: below this many outcomes, a model is "untested" and must be
    # excluded from win-rate reporting. Without this guard, a fallback model
    # (e.g. duckduckgo) with a handful of early failures gets a 0.0 win rate
    # and is effectively locked out of future selection.
    _COLD_START_THRESHOLD = 20

    def get_model_win_rates(self, min_samples: int = 20) -> dict[str, float]:
        threshold = max(min_samples, self._COLD_START_THRESHOLD)
        entries = self._load_all()
        counts: dict[str, dict] = {}
        for e in entries:
            model = e.get("model", "UNKNOWN")
            if model not in counts:
                counts[model] = {"total": 0, "errors": 0}
            counts[model]["total"] += 1
            if e.get("error"):
                counts[model]["errors"] += 1
        return {
            m: round(1.0 - (v["errors"] / v["total"]), 3)
            for m, v in counts.items()
            if v["total"] >= threshold
        }

    def summary(self) -> dict:
        all_entries = self._load_all()
        total = len(all_entries)
        if not total:
            return {"total_interactions": 0}
        model_counts: dict[str, int] = {}
        error_count = 0
        for e in all_entries:
            model_counts[e.get("model", "?")] = model_counts.get(e.get("model", "?"), 0) + 1
            if e.get("error"):
                error_count += 1
        return {
            "total_interactions":  total,
            "model_distribution":  model_counts,
            "error_count":         error_count,
            "error_rate_pct":      round(error_count / total * 100, 1),
        }

    def normalized_summary(self) -> dict:
        all_entries = self._load_all()
        total = len(all_entries)
        if not total:
            return {"total_interactions": 0}
        raw_counts: dict[str, int]        = {}
        normalized_counts: dict[str, int] = {}
        route_counts: dict[str, int]      = {}
        error_count = 0
        for e in all_entries:
            raw_model = e.get("model", "?")
            raw_counts[raw_model] = raw_counts.get(raw_model, 0) + 1
            norm = _normalize_model(raw_model)
            normalized_counts[norm] = normalized_counts.get(norm, 0) + 1
            route = e.get("routed_by", "?")
            route_counts[route] = route_counts.get(route, 0) + 1
            if e.get("error"):
                error_count += 1
        return {
            "total_interactions":    total,
            "model_distribution":    normalized_counts,
            "raw_model_distribution": raw_counts,
            "route_distribution":    route_counts,
            "error_count":           error_count,
            "error_rate_pct":        round(error_count / total * 100, 1),
        }

    def get_error_breakdown(self, hours: float = 24.0) -> dict:
        cutoff = time.time() - hours * 3600
        all_entries = self._load_all(hours=hours)
        window      = [e for e in all_entries if e.get("ts", 0) >= cutoff]
        error_entries = [e for e in window if e.get("error")]
        breakdown: dict[str, int] = {}
        for e in error_entries:
            cat = e.get("error_category", "unknown")
            breakdown[cat] = breakdown.get(cat, 0) + 1
        total_requests = len(window)
        error_rate = round(len(error_entries) / max(total_requests, 1) * 100, 1)
        return {
            "hours":          hours,
            "total":          len(error_entries),
            "breakdown":      breakdown,
            "error_rate_pct": error_rate,
        }

    def get_error_rates_by_route(self, last_n: int = 50) -> dict[str, float]:
        """
        Return {routed_by → error_rate} for the last `last_n` dispatches.
        Only routes with >= 3 samples are included. Used by the dispatcher to
        skip routes that have been consistently failing (> 60% error rate).
        """
        with _pg_lock:
            pg_ok = _pg_enabled
        if pg_ok:
            conn = _pg_conn()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT routed_by,
                                   COUNT(*) FILTER (WHERE error) AS errs,
                                   COUNT(*) AS total
                            FROM (
                                SELECT routed_by, error
                                FROM agent_insights
                                ORDER BY ts DESC
                                LIMIT %s
                            ) sub
                            GROUP BY routed_by
                        """, (last_n,))
                        rows = cur.fetchall()
                    conn.close()
                    return {
                        r[0]: round(r[1] / r[2], 3)
                        for r in rows if r[2] >= 3
                    }
                except Exception:
                    try: conn.close()
                    except Exception: pass
        # File fallback
        entries = self._load_all()[-last_n:]
        route_stats: dict[str, dict] = {}
        for e in entries:
            route = e.get("routed_by", "unknown")
            if route not in route_stats:
                route_stats[route] = {"total": 0, "errors": 0}
            route_stats[route]["total"] += 1
            if e.get("error"):
                route_stats[route]["errors"] += 1
        return {
            r: round(v["errors"] / v["total"], 3)
            for r, v in route_stats.items()
            if v["total"] >= 3
        }

    def get_latency_percentiles(self, hours: float = 24.0) -> dict:
        cutoff = time.time() - hours * 3600
        latencies_ms = sorted([
            e["latency_ms"] for e in self._load_all(hours=hours)
            if e.get("ts", 0) >= cutoff and "latency_ms" in e
        ])
        if not latencies_ms:
            return {"hours": hours, "samples": 0,
                    "p50_s": 0, "p95_s": 0, "p99_s": 0, "avg_s": 0, "max_s": 0}
        def pct(p):
            idx = int(len(latencies_ms) * p / 100)
            return round(latencies_ms[min(idx, len(latencies_ms) - 1)] / 1000, 3)
        return {
            "hours":   hours,
            "samples": len(latencies_ms),
            "p50_s":   pct(50),
            "p95_s":   pct(95),
            "p99_s":   pct(99),
            "avg_s":   round(sum(latencies_ms) / len(latencies_ms) / 1000, 3),
            "max_s":   round(latencies_ms[-1] / 1000, 3),
        }

    def record_feedback(self, session_id: str, message: str, is_error: bool,
                        rating: Optional[int] = None) -> bool:
        """Mark the most recent matching entry as error/win. Returns True if found."""
        # Try PG first
        with _pg_lock:
            pg_ok = _pg_enabled
        if pg_ok:
            conn = _pg_conn()
            if conn:
                try:
                    prefix = message[:200].lower()
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE agent_insights
                                SET error = %s,
                                    feedback_applied = TRUE,
                                    error_category = CASE
                                        WHEN %s AND error_category IS NULL THEN 'user_rated_bad'
                                        ELSE error_category END,
                                    rating = COALESCE(%s, rating)
                                WHERE id = (
                                    SELECT id FROM agent_insights
                                    WHERE session_id = %s
                                    ORDER BY ts DESC LIMIT 1
                                )
                            """, (is_error, is_error, rating, session_id))
                    conn.close()
                    return True
                except Exception:
                    try: conn.close()
                    except: pass
        # File fallback
        try:
            all_entries = self._load_all()
            msg_prefix = message[:80].lower()
            for entry in reversed(all_entries):
                if (
                    entry.get("session_id") == session_id
                    and entry.get("message", "")[:80].lower() == msg_prefix
                ):
                    entry["error"] = is_error
                    entry["feedback_applied"] = True
                    if rating: entry["rating"] = rating
                    if is_error and not entry.get("error_category"):
                        entry["error_category"] = "user_rated_bad"
                    try:
                        with open(LOG_PATH, "w", encoding="utf-8") as f:
                            json.dump(all_entries, f)
                        return True
                    except Exception:
                        return False
        except Exception:
            pass
        return False


# Singleton — init kicks off PG table creation in background
insight_log = InsightLog()
