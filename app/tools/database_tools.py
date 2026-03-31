"""
Database tools — Super Agent can inspect and query its own memory store.

Supports PostgreSQL (primary via DATABASE_URL) and SQLite fallback.
Write tools require owner safe word authorization.

Use cases:
  - Inspect session memory (what conversations are stored)
  - Check insight log data (error rates, model performance)
  - Diagnose database connectivity issues
  - Clean up old sessions (authorized)
"""
import os
from langchain_core.tools import tool
from ..memory.session import DB_PATH


def _get_conn():
    """Return a database connection matching the current DB_PATH."""
    if DB_PATH.startswith("postgresql://"):
        import psycopg2
        return psycopg2.connect(DB_PATH), "pg"
    else:
        import sqlite3
        db_file = DB_PATH.replace("sqlite:///", "")
        return sqlite3.connect(db_file), "sqlite"


def _query(sql: str, params=None) -> list[dict]:
    """Execute a SELECT query and return rows as list of dicts."""
    conn, _ = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _execute(sql: str, params=None) -> int:
    """Execute a write query, return rowcount."""
    conn, _ = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or [])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── Read tools ────────────────────────────────────────────────────────────────

@tool
def db_health_check(dummy: str = "") -> str:
    """
    Check database connectivity and report basic stats.
    Returns: DB type, connection status, session count.
    """
    try:
        db_type = "PostgreSQL" if DB_PATH.startswith("postgresql://") else "SQLite"
        rows = _query("SELECT COUNT(*) as cnt FROM message_store")
        count = rows[0]["cnt"] if rows else 0
        return f"DB: {db_type} | Status: Connected | Stored messages: {count}"
    except Exception as e:
        return f"[DB health error: {e}]"


@tool
def db_list_sessions(dummy: str = "") -> str:
    """List all active session IDs and their message counts."""
    try:
        rows = _query(
            "SELECT session_id, COUNT(*) as msg_count "
            "FROM message_store GROUP BY session_id ORDER BY msg_count DESC LIMIT 20"
        )
        if not rows:
            return "No sessions found in database."
        lines = [f"session_id: {r['session_id']} — {r['msg_count']} messages" for r in rows]
        return "\n".join(lines)
    except Exception as e:
        return f"[DB error: {e}]"


@tool
def db_get_session_preview(session_id: str) -> str:
    """Get the last 5 messages from a session for inspection."""
    try:
        rows = _query(
            "SELECT type, content FROM message_store "
            "WHERE session_id = %s ORDER BY id DESC LIMIT 5" if DB_PATH.startswith("postgresql://")
            else "SELECT type, content FROM message_store "
                 "WHERE session_id = ? ORDER BY id DESC LIMIT 5",
            [session_id],
        )
        if not rows:
            return f"No messages found for session '{session_id}'"
        lines = [f"[{r['type'].upper()}] {str(r['content'])[:200]}" for r in reversed(rows)]
        return "\n".join(lines)
    except Exception as e:
        return f"[DB error: {e}]"


@tool
def db_get_error_stats(dummy: str = "") -> str:
    """
    Read the insight log and report error rates by model.
    Shows which models are failing most frequently.
    """
    import json
    from ..learning.insight_log import LOG_PATH
    try:
        if not os.path.exists(LOG_PATH):
            return "No insight log found yet — no interactions recorded."
        with open(LOG_PATH, "r") as f:
            entries = json.load(f)
        if not entries:
            return "Insight log is empty."
        total = len(entries)
        errors = [e for e in entries if e.get("error")]
        model_errors: dict[str, int] = {}
        model_total: dict[str, int] = {}
        for e in entries[-200:]:
            m = e.get("model", "?")
            model_total[m] = model_total.get(m, 0) + 1
            if e.get("error"):
                model_errors[m] = model_errors.get(m, 0) + 1
        lines = [f"Total interactions: {total} | Total errors: {len(errors)} ({len(errors)/total*100:.1f}%)"]
        for m in sorted(model_total, key=lambda k: model_total[k], reverse=True):
            err = model_errors.get(m, 0)
            tot = model_total[m]
            lines.append(f"  {m}: {err}/{tot} errors ({err/tot*100:.1f}%)")
        return "\n".join(lines)
    except Exception as e:
        return f"[Insight log error: {e}]"


@tool
def db_get_failure_patterns(dummy: str = "") -> str:
    """
    Scan the insight log for recurring failure patterns.
    Returns: top failing models, peak error times, repeated error signatures.
    """
    import json
    from ..learning.insight_log import LOG_PATH
    try:
        if not os.path.exists(LOG_PATH):
            return "No insight log found."
        with open(LOG_PATH, "r") as f:
            entries = json.load(f)
        error_entries = [e for e in entries if e.get("error")]
        if not error_entries:
            return "No errors recorded — system is healthy."
        model_err: dict[str, int] = {}
        routed_err: dict[str, int] = {}
        for e in error_entries[-100:]:
            m = e.get("model", "?")
            r = e.get("routed_by", "?")
            model_err[m] = model_err.get(m, 0) + 1
            routed_err[r] = routed_err.get(r, 0) + 1
        top_model = max(model_err, key=lambda k: model_err[k]) if model_err else "none"
        top_route = max(routed_err, key=lambda k: routed_err[k]) if routed_err else "none"
        recent_rate = len(error_entries[-20:]) / 20 * 100
        return (
            f"Failure patterns (last 100 errors):\n"
            f"  Most failing model: {top_model} ({model_err.get(top_model, 0)} errors)\n"
            f"  Most failing route: {top_route} ({routed_err.get(top_route, 0)} errors)\n"
            f"  Recent error rate (last 20): {recent_rate:.0f}%\n"
            f"  Total errors recorded: {len(error_entries)}"
        )
    except Exception as e:
        return f"[Pattern scan error: {e}]"


# ── Write tools (require owner safe word via dispatcher) ──────────────────────

@tool
def db_clear_session(session_id: str) -> str:
    """
    Delete all messages for a specific session from the database.
    Use to clean up stale or corrupted sessions. Requires owner authorization.
    """
    try:
        placeholder = "%s" if DB_PATH.startswith("postgresql://") else "?"
        count = _execute(
            f"DELETE FROM message_store WHERE session_id = {placeholder}",
            [session_id],
        )
        return f"Cleared {count} messages from session '{session_id}'"
    except Exception as e:
        return f"[DB clear error: {e}]"


@tool
def db_run_safe_query(sql: str) -> str:
    """
    Run a read-only SQL query against the database for diagnostic purposes.
    Only SELECT statements are permitted. Requires owner authorization.
    """
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        return "[Blocked: only SELECT queries are permitted via this tool]"
    try:
        rows = _query(sql)
        if not rows:
            return "(query returned no rows)"
        lines = []
        if rows:
            lines.append(" | ".join(rows[0].keys()))
            lines.append("-" * 40)
        for r in rows[:50]:
            lines.append(" | ".join(str(v)[:80] for v in r.values()))
        return "\n".join(lines)
    except Exception as e:
        return f"[DB query error: {e}]"
