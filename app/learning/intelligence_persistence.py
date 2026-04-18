"""
PostgreSQL persistence for the intelligence scoring and leaderboard systems.

Provides four tables:
  intelligence_state           — single-row XP / prediction snapshot
  agent_leaderboard_state      — per-agent performance counters
  intelligence_xp_history      — append-only XP growth log (for chart)
  intelligence_daily_challenges — daily challenge progress

All public functions are safe to call when DATABASE_URL is absent — they
no-op silently and never raise. DB writes always run in background threads
so they never block the request path.
"""
import json
import os
import threading
import time

_AGENTS = ["N8N", "SHELL", "GITHUB", "SELF_IMPROVE"]

# ── Connection helper ─────────────────────────────────────────────────────────

def _pg_conn():
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        return None
    try:
        import psycopg2
        return psycopg2.connect(raw.replace("postgres://", "postgresql://", 1))
    except Exception:
        return None


# ── Table setup ───────────────────────────────────────────────────────────────

_tables_ready = False
_tables_lock  = threading.Lock()


def _ensure_tables() -> bool:
    global _tables_ready
    if _tables_ready:
        return True
    with _tables_lock:
        if _tables_ready:
            return True
        conn = _pg_conn()
        if not conn:
            return False
        try:
            with conn:
                with conn.cursor() as cur:
                    # ── Intelligence state (single row, id=1) ────────────
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS intelligence_state (
                            id                  INTEGER PRIMARY KEY DEFAULT 1,
                            xp                  INTEGER DEFAULT 0,
                            total_predictions   INTEGER DEFAULT 0,
                            correct_predictions INTEGER DEFAULT 0,
                            agent_calls         INTEGER DEFAULT 0,
                            vault_writes        INTEGER DEFAULT 0,
                            patterns_discovered INTEGER DEFAULT 0,
                            achievements        JSONB   DEFAULT '{}',
                            xp_log              JSONB   DEFAULT '[]',
                            combo_streak        INTEGER DEFAULT 0,
                            best_combo          INTEGER DEFAULT 0,
                            updated_at          DOUBLE PRECISION DEFAULT 0
                        )
                    """)
                    # ── Per-agent leaderboard ────────────────────────────
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS agent_leaderboard_state (
                            agent           VARCHAR(32) PRIMARY KEY,
                            calls           INTEGER          DEFAULT 0,
                            total_ms        DOUBLE PRECISION DEFAULT 0,
                            errors          INTEGER          DEFAULT 0,
                            sessions        JSONB            DEFAULT '[]',
                            pred_correct    INTEGER          DEFAULT 0,
                            pred_total      INTEGER          DEFAULT 0,
                            streak_current  INTEGER          DEFAULT 0,
                            streak_best     INTEGER          DEFAULT 0,
                            last_call_ts    DOUBLE PRECISION DEFAULT 0,
                            hourly_calls    JSONB            DEFAULT '[]',
                            updated_at      DOUBLE PRECISION DEFAULT 0
                        )
                    """)
                    # ── XP growth history (append-only) ──────────────────
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS intelligence_xp_history (
                            id          SERIAL PRIMARY KEY,
                            ts          DOUBLE PRECISION NOT NULL,
                            xp_total    INTEGER          NOT NULL,
                            level       INTEGER          NOT NULL,
                            event       VARCHAR(64),
                            xp_delta    INTEGER          DEFAULT 0,
                            created_at  TIMESTAMPTZ      DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_ixh_ts
                            ON intelligence_xp_history(ts DESC)
                    """)
                    # ── Daily challenges ──────────────────────────────────
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS intelligence_daily_challenges (
                            id              SERIAL PRIMARY KEY,
                            date            DATE        NOT NULL,
                            challenge_key   VARCHAR(64) NOT NULL,
                            title           VARCHAR(128) NOT NULL,
                            description     TEXT,
                            target_value    INTEGER     NOT NULL,
                            current_value   INTEGER     DEFAULT 0,
                            xp_reward       INTEGER     NOT NULL,
                            completed       BOOLEAN     DEFAULT FALSE,
                            completed_at    DOUBLE PRECISION,
                            UNIQUE(date, challenge_key)
                        )
                    """)
            _tables_ready = True
            return True
        except Exception:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ── Intelligence state load / save ────────────────────────────────────────────

def load_intelligence_state() -> dict | None:
    """Return persisted XP/prediction state, or None if DB unavailable/empty."""
    if not _ensure_tables():
        return None
    conn = _pg_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT xp, total_predictions, correct_predictions, agent_calls,
                       vault_writes, patterns_discovered, achievements, xp_log,
                       combo_streak, best_combo
                FROM intelligence_state WHERE id = 1
            """)
            row = cur.fetchone()
        if not row:
            return None
        return {
            "xp":                   row[0] or 0,
            "total_predictions":    row[1] or 0,
            "correct_predictions":  row[2] or 0,
            "agent_calls":          row[3] or 0,
            "vault_writes":         row[4] or 0,
            "patterns_discovered":  row[5] or 0,
            "achievements":         dict(row[6]) if row[6] else {},
            "xp_log":               list(row[7]) if row[7] else [],
            "combo_streak":         row[8] or 0,
            "best_combo":           row[9] or 0,
        }
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def save_intelligence_state(state: dict) -> None:
    """Upsert intelligence state. Always called from a background thread."""
    if not _ensure_tables():
        return
    conn = _pg_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intelligence_state
                        (id, xp, total_predictions, correct_predictions,
                         agent_calls, vault_writes, patterns_discovered,
                         achievements, xp_log, combo_streak, best_combo, updated_at)
                    VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        xp                  = EXCLUDED.xp,
                        total_predictions   = EXCLUDED.total_predictions,
                        correct_predictions = EXCLUDED.correct_predictions,
                        agent_calls         = EXCLUDED.agent_calls,
                        vault_writes        = EXCLUDED.vault_writes,
                        patterns_discovered = EXCLUDED.patterns_discovered,
                        achievements        = EXCLUDED.achievements,
                        xp_log              = EXCLUDED.xp_log,
                        combo_streak        = EXCLUDED.combo_streak,
                        best_combo          = EXCLUDED.best_combo,
                        updated_at          = EXCLUDED.updated_at
                """, (
                    state.get("xp", 0),
                    state.get("total_predictions", 0),
                    state.get("correct_predictions", 0),
                    state.get("agent_calls", 0),
                    state.get("vault_writes", 0),
                    state.get("patterns_discovered", 0),
                    json.dumps(state.get("achievements", {})),
                    json.dumps(list(state.get("xp_log", []))[-50:]),
                    state.get("combo_streak", 0),
                    state.get("best_combo", 0),
                    time.time(),
                ))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Leaderboard state load / save ─────────────────────────────────────────────

def load_leaderboard_state() -> dict | None:
    """Return {agent: stats_dict} from DB, or None if unavailable/empty."""
    if not _ensure_tables():
        return None
    conn = _pg_conn()
    if not conn:
        return None
    result: dict = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT agent, calls, total_ms, errors, sessions, pred_correct,
                       pred_total, streak_current, streak_best, last_call_ts, hourly_calls
                FROM agent_leaderboard_state
            """)
            for row in cur.fetchall():
                result[row[0]] = {
                    "calls":          row[1] or 0,
                    "total_ms":       row[2] or 0.0,
                    "errors":         row[3] or 0,
                    "sessions":       set(row[4]) if row[4] else set(),
                    "pred_correct":   row[5] or 0,
                    "pred_total":     row[6] or 0,
                    "streak_current": row[7] or 0,
                    "streak_best":    row[8] or 0,
                    "last_call_ts":   row[9] or 0.0,
                    "hourly_calls":   list(row[10]) if row[10] else [],
                }
        return result or None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def save_leaderboard_state(stats: dict) -> None:
    """Upsert all agent rows. Always called from a background thread."""
    if not _ensure_tables():
        return
    conn = _pg_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                for agent, s in stats.items():
                    sessions_list = list(s.get("sessions", set()))
                    # keep only last 2 hours of hourly_calls timestamps
                    cutoff = time.time() - 7200
                    hourly = [t for t in s.get("hourly_calls", []) if t > cutoff]
                    cur.execute("""
                        INSERT INTO agent_leaderboard_state
                            (agent, calls, total_ms, errors, sessions, pred_correct,
                             pred_total, streak_current, streak_best, last_call_ts,
                             hourly_calls, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (agent) DO UPDATE SET
                            calls           = EXCLUDED.calls,
                            total_ms        = EXCLUDED.total_ms,
                            errors          = EXCLUDED.errors,
                            sessions        = EXCLUDED.sessions,
                            pred_correct    = EXCLUDED.pred_correct,
                            pred_total      = EXCLUDED.pred_total,
                            streak_current  = EXCLUDED.streak_current,
                            streak_best     = EXCLUDED.streak_best,
                            last_call_ts    = EXCLUDED.last_call_ts,
                            hourly_calls    = EXCLUDED.hourly_calls,
                            updated_at      = EXCLUDED.updated_at
                    """, (
                        agent,
                        s.get("calls", 0),
                        s.get("total_ms", 0.0),
                        s.get("errors", 0),
                        json.dumps(sessions_list),
                        s.get("pred_correct", 0),
                        s.get("pred_total", 0),
                        s.get("streak_current", 0),
                        s.get("streak_best", 0),
                        s.get("last_call_ts", 0.0),
                        json.dumps(hourly),
                        time.time(),
                    ))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── XP history (append-only, for chart) ──────────────────────────────────────

def append_xp_history(xp_total: int, level: int, event: str, xp_delta: int) -> None:
    """Insert one XP history row in background — never blocks the caller."""
    def _do():
        if not _ensure_tables():
            return
        conn = _pg_conn()
        if not conn:
            return
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO intelligence_xp_history
                            (ts, xp_total, level, event, xp_delta)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (time.time(), xp_total, level, (event or "")[:64], xp_delta))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    threading.Thread(target=_do, daemon=True).start()


def get_xp_history(limit: int = 120) -> list[dict]:
    """Return last `limit` XP events, oldest-first, for the growth chart."""
    if not _ensure_tables():
        return []
    conn = _pg_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts, xp_total, level, event, xp_delta
                FROM intelligence_xp_history
                ORDER BY ts DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [
            {"ts": r[0], "xp_total": r[1], "level": r[2], "event": r[3], "xp_delta": r[4]}
            for r in reversed(rows)
        ]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Background auto-save (every 60 s) ────────────────────────────────────────

_bg_save_started = False
_bg_save_lock    = threading.Lock()


def start_background_save() -> None:
    """Start the 60-second save daemon. Safe to call multiple times."""
    global _bg_save_started
    if _bg_save_started:
        return
    with _bg_save_lock:
        if _bg_save_started:
            return
        _bg_save_started = True

    def _loop():
        # Give tables time to initialise before first save
        for _ in range(15):
            if _ensure_tables():
                break
            time.sleep(2)
        while True:
            time.sleep(60)
            try:
                from .intelligence_score import _state as _iq_state
                save_intelligence_state(_iq_state)
            except Exception:
                pass
            try:
                from .agent_leaderboard import _stats as _lb_stats
                save_leaderboard_state(_lb_stats)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True, name="intelligence-persist").start()


# ── Kick off table init immediately on import ─────────────────────────────────
threading.Thread(target=_ensure_tables, daemon=True).start()
