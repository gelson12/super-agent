"""
Daily challenge system.

Generates 3 challenges per UTC day, deterministically chosen from a pool
based on the date hash so they change every day but are reproducible.
Progress is tracked in-memory (fast) and flushed to PostgreSQL.
Completed challenges award bonus XP via intelligence_score.

Challenge categories:
  calls_*      — make N agent calls today
  correct_*    — get N correct predictions today
  diversity_*  — use N different agents today
  speed_*      — get N responses under 10 s today
  combo_*      — reach a combo streak of N today
"""
import datetime
import os
import threading
import time

# ── Challenge pool ────────────────────────────────────────────────────────────
# (key, title, description, target, xp_reward)
_POOL = [
    # Calls
    ("calls_5",     "📞 ACTIVE DAY",      "Make 5 agent calls today",        5,   50),
    ("calls_10",    "📞 BUSY DAY",         "Make 10 agent calls today",       10,  75),
    ("calls_20",    "📞 POWER USER",       "Make 20 agent calls today",       20, 100),
    # Correct predictions
    ("correct_3",   "🎯 SHARPSHOOTER",    "3 correct predictions today",      3,   50),
    ("correct_5",   "🎯 MARKSMAN",        "5 correct predictions today",      5,   75),
    ("correct_8",   "🎯 SNIPER",          "8 correct predictions today",      8,  100),
    # Diversity
    ("diversity_2", "🌐 WELL ROUNDED",    "Use 2 different agents today",     2,   40),
    ("diversity_3", "🌐 VERSATILE",       "Use 3 different agents today",     3,   60),
    ("diversity_4", "🌐 OMNIDIRECTIONAL", "Use all 4 agents today",           4,  100),
    # Speed
    ("speed_3",     "⚡ QUICKDRAW",       "3 responses under 10 s today",    3,   50),
    ("speed_5",     "⚡ SPEED DEMON",     "5 responses under 10 s today",    5,   75),
    # Combo
    ("combo_3",     "🔥 COMBO STARTER",   "Hit a 3× prediction streak",      3,   60),
    ("combo_5",     "🔥 COMBO MASTER",    "Hit a 5× prediction streak",      5,  100),
]

# ── In-memory state ───────────────────────────────────────────────────────────
_lock             = threading.Lock()
_today_date: str | None = None
_challenges: list[dict] = []

# Rolling counters (reset each UTC day)
_calls_today:    int = 0
_correct_today:  int = 0
_agents_today:   set = set()
_speed_today:    int = 0
_combo_peak:     int = 0


def _utc_today() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _pick_challenges(date_str: str) -> list[dict]:
    """Choose 3 challenges deterministically from the pool using the date as seed."""
    import hashlib
    seed = int(hashlib.md5(date_str.encode()).hexdigest(), 16)

    call_pool  = [c for c in _POOL if c[0].startswith("calls_")]
    pred_pool  = [c for c in _POOL if c[0].startswith("correct_") or c[0].startswith("diversity_")]
    bonus_pool = [c for c in _POOL if c[0].startswith("speed_") or c[0].startswith("combo_")]

    picks = [
        call_pool[seed % len(call_pool)],
        pred_pool[(seed >> 8) % len(pred_pool)],
        bonus_pool[(seed >> 16) % len(bonus_pool)],
    ]
    return [
        {
            "key":         p[0],
            "title":       p[1],
            "description": p[2],
            "target":      p[3],
            "xp_reward":   p[4],
            "current":     0,
            "completed":   False,
            "completed_at": None,
        }
        for p in picks
    ]


def _ensure_today() -> None:
    """Refresh state if the UTC day has rolled over (call while holding _lock)."""
    global _today_date, _challenges
    global _calls_today, _correct_today, _agents_today, _speed_today, _combo_peak

    today = _utc_today()
    if _today_date == today:
        return

    _today_date      = today
    _calls_today     = 0
    _correct_today   = 0
    _agents_today    = set()
    _speed_today     = 0
    _combo_peak      = 0

    # Try to restore from DB first
    persisted = _load_from_db(today)
    if persisted is not None:
        _challenges = persisted
    else:
        _challenges = _pick_challenges(today)
        _save_to_db(_challenges, today)


def _advance(key_prefix: str, value: int) -> list[str]:
    """
    Update `current` for any incomplete challenge whose key starts with key_prefix.
    Returns list of keys that were just completed.
    """
    completed_keys = []
    for ch in _challenges:
        if ch["completed"]:
            continue
        cat = ch["key"].split("_")[0] + "_"
        if not ch["key"].startswith(cat) or cat != key_prefix:
            continue
        ch["current"] = max(ch["current"], value)
        if ch["current"] >= ch["target"]:
            ch["completed"]    = True
            ch["completed_at"] = time.time()
            completed_keys.append(ch["key"])
    return completed_keys


def _award_xp(keys: list[str]) -> None:
    """Award XP to intelligence_score for completed challenges."""
    for key in keys:
        ch = next((c for c in _challenges if c["key"] == key), None)
        if not ch:
            continue
        try:
            from .intelligence_score import _add_xp, _state, _lock as _iq_lock, _check_achievements
            with _iq_lock:
                _add_xp(ch["xp_reward"], f"challenge:{ch['title']}")
            _check_achievements()
        except Exception:
            pass
    if keys:
        _save_to_db(_challenges, _today_date)


def get_challenges() -> list[dict]:
    """Return today's challenges (thread-safe copy)."""
    with _lock:
        _ensure_today()
        return [dict(c) for c in _challenges]


def record_agent_call(agent_key: str, elapsed_ms: float) -> None:
    """Call after every agent response to update daily progress."""
    global _calls_today, _agents_today, _speed_today
    with _lock:
        _ensure_today()
        _calls_today += 1
        _agents_today.add(agent_key.upper().replace("-", "_"))
        if elapsed_ms < 10_000:
            _speed_today += 1

        done = []
        done += _advance("calls_",     _calls_today)
        done += _advance("diversity_", len(_agents_today))
        done += _advance("speed_",     _speed_today)
    _award_xp(done)


def record_correct_prediction() -> None:
    """Call when a prediction is correct."""
    global _correct_today
    with _lock:
        _ensure_today()
        _correct_today += 1
        done = _advance("correct_", _correct_today)
    _award_xp(done)


def record_combo_update(combo: int) -> None:
    """Call whenever the active combo streak changes."""
    global _combo_peak
    with _lock:
        _ensure_today()
        if combo > _combo_peak:
            _combo_peak = combo
        done = _advance("combo_", _combo_peak)
    _award_xp(done)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _pg_conn():
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        return None
    try:
        import psycopg2
        return psycopg2.connect(raw.replace("postgres://", "postgresql://", 1))
    except Exception:
        return None


def _load_from_db(date_str: str) -> list[dict] | None:
    conn = _pg_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT challenge_key, title, description, target_value,
                       current_value, xp_reward, completed, completed_at
                FROM intelligence_daily_challenges
                WHERE date = %s ORDER BY id
            """, (date_str,))
            rows = cur.fetchall()
        if not rows:
            return None
        return [
            {
                "key":          r[0],
                "title":        r[1],
                "description":  r[2],
                "target":       r[3],
                "current":      r[4],
                "xp_reward":    r[5],
                "completed":    r[6],
                "completed_at": r[7],
            }
            for r in rows
        ]
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _save_to_db(challenges: list[dict], date_str: str | None) -> None:
    if not date_str:
        return
    conn = _pg_conn()
    if not conn:
        return

    def _do():
        try:
            with conn:
                with conn.cursor() as cur:
                    for ch in challenges:
                        cur.execute("""
                            INSERT INTO intelligence_daily_challenges
                                (date, challenge_key, title, description,
                                 target_value, current_value, xp_reward,
                                 completed, completed_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (date, challenge_key) DO UPDATE SET
                                current_value = EXCLUDED.current_value,
                                completed     = EXCLUDED.completed,
                                completed_at  = EXCLUDED.completed_at
                        """, (
                            date_str,
                            ch["key"],
                            ch["title"],
                            ch.get("description", ""),
                            ch["target"],
                            ch["current"],
                            ch["xp_reward"],
                            ch["completed"],
                            ch.get("completed_at"),
                        ))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=_do, daemon=True).start()
