"""
Gamified intelligence scoring engine.

Tracks XP, levels, and achievements as the system accumulates learned patterns,
correct predictions, and agent experience. Zero API cost — pure in-memory counters
persisted to PostgreSQL every 60 s (via intelligence_persistence).

XP sources:
  correct_prediction  +10 (×combo multiplier)   New pattern discovered  +5
  agent_call          +1                          Vault write             +2
  achievement unlock  +25–500

Combo multiplier (consecutive correct predictions):
  1–2   →  ×1.0  (+10)
  3–4   →  ×1.5  (+15)
  5–7   →  ×2.0  (+20)
  8–10  →  ×2.5  (+25)
  11+   →  ×3.0  (+30)
"""
import threading
import time

# Level thresholds (cumulative XP to reach that level)
_LEVEL_THRESHOLDS = [0, 100, 250, 500, 800, 1200, 1800, 2600, 3600, 5000]
_LEVEL_NAMES = [
    "NEWBORN", "AWARE", "LEARNING", "ADAPTING", "PERCEPTIVE",
    "INTUITIVE", "PREDICTIVE", "PRESCIENT", "ORACLE", "OMNISCIENT",
]

XP_REWARDS = {
    "correct_prediction":  10,
    "wrong_prediction":     0,
    "new_pattern":          5,
    "agent_call":           1,
    "vault_write":          2,
}

ALL_ACHIEVEMENTS = {
    "first_prediction":    {"name": "FIRST STEPS",        "desc": "Made the first prediction",       "icon": "🎯", "xp": 25},
    "first_correct":       {"name": "MIND READER",        "desc": "First correct prediction",        "icon": "🧠", "xp": 50},
    "ten_correct":         {"name": "LEARNING MACHINE",   "desc": "10 correct predictions",          "icon": "⚡", "xp": 100},
    "fifty_correct":       {"name": "ORACLE",             "desc": "50 correct predictions",          "icon": "🔮", "xp": 200},
    "pattern_seeker":      {"name": "PATTERN SEEKER",     "desc": "10 sequence patterns stored",     "icon": "🔍", "xp": 50},
    "pattern_master":      {"name": "PATTERN MASTER",     "desc": "50 sequence patterns stored",     "icon": "🏛️",  "xp": 100},
    "time_lord":           {"name": "TIME LORD",          "desc": "Time-based patterns active",      "icon": "⏰", "xp": 75},
    "transition_master":   {"name": "TRANSITION MASTER",  "desc": "5+ agent transition patterns",    "icon": "🔀", "xp": 75},
    "vault_keeper":        {"name": "VAULT KEEPER",       "desc": "100 vault outcome writes",        "icon": "📚", "xp": 75},
    "session_sage":        {"name": "SESSION SAGE",       "desc": "50 unique sessions tracked",      "icon": "💫", "xp": 100},
    "accuracy_50":         {"name": "SHARP MIND",         "desc": "50%+ prediction accuracy (20+ samples)", "icon": "🎖️",  "xp": 100},
    "accuracy_75":         {"name": "CLAIRVOYANT",        "desc": "75%+ prediction accuracy (20+ samples)", "icon": "✨", "xp": 200},
    "level_5":             {"name": "PERCEPTIVE",         "desc": "Reached Level 5",                 "icon": "⭐", "xp": 150},
    "level_8":             {"name": "PRESCIENT",          "desc": "Reached Level 8",                 "icon": "🌟", "xp": 250},
    "omniscient":          {"name": "OMNISCIENT",         "desc": "Reached maximum Level 10",        "icon": "👑", "xp": 500},
    # Combo achievements
    "combo_5":             {"name": "ON FIRE",            "desc": "5× prediction combo streak",      "icon": "🔥", "xp": 75},
    "combo_10":            {"name": "UNSTOPPABLE",        "desc": "10× prediction combo streak",     "icon": "💥", "xp": 150},
}

_state: dict = {
    "xp":                   0,
    "total_predictions":    0,
    "correct_predictions":  0,
    "agent_calls":          0,
    "vault_writes":         0,
    "patterns_discovered":  0,
    "sessions":             set(),
    "achievements":         {},   # key → {"ts", "name", "desc", "icon"}
    "xp_log":               [],   # last 50 events
    "combo_streak":         0,    # current consecutive correct predictions
    "best_combo":           0,    # all-time best streak
}
_lock = threading.Lock()


# ── Combo multiplier ──────────────────────────────────────────────────────────

def _combo_multiplier(streak: int) -> int:
    """Return XP amount for a correct prediction based on current streak."""
    if streak >= 11: return 30
    if streak >= 8:  return 25
    if streak >= 5:  return 20
    if streak >= 3:  return 15
    return 10


# ── XP helper ─────────────────────────────────────────────────────────────────

def _add_xp(amount: int, event: str) -> None:
    """Add XP to _state and log the event. Caller must hold _lock."""
    _state["xp"] += amount
    entry = {"ts": round(time.time()), "event": event, "xp": amount}
    _state["xp_log"].append(entry)
    if len(_state["xp_log"]) > 50:
        _state["xp_log"].pop(0)
    # Fire-and-forget XP history row for chart
    try:
        from .intelligence_persistence import append_xp_history
        lvl = get_level_info()["level"]
        append_xp_history(_state["xp"], lvl, event, amount)
    except Exception:
        pass


# ── Level info ────────────────────────────────────────────────────────────────

def get_level_info() -> dict:
    xp = _state["xp"]
    level = 1
    for i, threshold in enumerate(_LEVEL_THRESHOLDS):
        if xp >= threshold:
            level = i + 1
    level = min(level, 10)
    name = _LEVEL_NAMES[level - 1]
    current_floor = _LEVEL_THRESHOLDS[level - 1]
    next_ceiling  = _LEVEL_THRESHOLDS[level] if level < 10 else _LEVEL_THRESHOLDS[-1]
    xp_in_level   = xp - current_floor
    xp_span       = next_ceiling - current_floor
    pct = min(100, round(xp_in_level / xp_span * 100, 1)) if xp_span > 0 else 100
    return {
        "level":        level,
        "name":         name,
        "xp_total":     xp,
        "xp_in_level":  xp_in_level,
        "xp_to_next":   xp_span - xp_in_level,
        "xp_span":      xp_span,
        "progress_pct": pct,
        "is_max":       level == 10,
    }


# ── Public record functions ───────────────────────────────────────────────────

def record_prediction(was_correct: bool) -> None:
    with _lock:
        _state["total_predictions"] += 1
        if was_correct:
            _state["correct_predictions"] += 1
            _state["combo_streak"] += 1
            if _state["combo_streak"] > _state["best_combo"]:
                _state["best_combo"] = _state["combo_streak"]
            xp_amount = _combo_multiplier(_state["combo_streak"])
            combo_now = _state["combo_streak"]
            _add_xp(xp_amount, "correct_prediction")
        else:
            _state["combo_streak"] = 0
            combo_now = 0

    if was_correct:
        try:
            from .daily_challenges import record_correct_prediction, record_combo_update
            record_correct_prediction()
            record_combo_update(combo_now)
        except Exception:
            pass

    _check_achievements()


def record_agent_call(session_id: str) -> None:
    with _lock:
        _state["agent_calls"] += 1
        _state["sessions"].add(session_id)
        _add_xp(XP_REWARDS["agent_call"], "agent_call")
    _check_achievements()


def record_vault_write() -> None:
    with _lock:
        _state["vault_writes"] += 1
        _add_xp(XP_REWARDS["vault_write"], "vault_write")
    _check_achievements()


def record_new_pattern() -> None:
    with _lock:
        _state["patterns_discovered"] += 1
        _add_xp(XP_REWARDS["new_pattern"], "new_pattern")
    _check_achievements()


# ── Achievements ──────────────────────────────────────────────────────────────

def _unlock(key: str) -> None:
    if key in _state["achievements"]:
        return
    ach = ALL_ACHIEVEMENTS.get(key)
    if not ach:
        return
    _state["achievements"][key] = {
        "ts":   round(time.time()),
        "name": ach["name"],
        "desc": ach["desc"],
        "icon": ach["icon"],
    }
    _add_xp(ach["xp"], f"achievement:{ach['name']}")


def _check_achievements() -> None:
    with _lock:
        total   = _state["total_predictions"]
        correct = _state["correct_predictions"]
        sessions = len(_state["sessions"])
        vw      = _state["vault_writes"]
        combo   = _state["best_combo"]

    if total   >= 1:  _unlock("first_prediction")
    if correct >= 1:  _unlock("first_correct")
    if correct >= 10: _unlock("ten_correct")
    if correct >= 50: _unlock("fifty_correct")
    if sessions >= 50: _unlock("session_sage")
    if vw >= 100:     _unlock("vault_keeper")
    if combo >= 5:    _unlock("combo_5")
    if combo >= 10:   _unlock("combo_10")

    if total >= 20:
        acc = correct / total
        if acc >= 0.50: _unlock("accuracy_50")
        if acc >= 0.75: _unlock("accuracy_75")

    try:
        from .trajectory_predictor import _sequence_store
        n = len(_sequence_store)
        if n >= 10: _unlock("pattern_seeker")
        if n >= 50: _unlock("pattern_master")
    except Exception:
        pass

    try:
        from .behavior_patterns import _time_counts, _transitions
        for _, counts in _time_counts.items():
            if sum(counts.values()) >= 5:
                _unlock("time_lord")
                break
        total_tr = sum(sum(v.values()) for v in _transitions.values())
        if total_tr >= 5: _unlock("transition_master")
    except Exception:
        pass

    lvl = get_level_info()["level"]
    if lvl >= 5:  _unlock("level_5")
    if lvl >= 8:  _unlock("level_8")
    if lvl >= 10: _unlock("omniscient")


# ── Stats for API ─────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with _lock:
        total    = _state["total_predictions"]
        correct  = _state["correct_predictions"]
        agent_calls = _state["agent_calls"]
        vw       = _state["vault_writes"]
        sessions = len(_state["sessions"])
        achievements = dict(_state["achievements"])
        xp_log   = list(_state["xp_log"][-20:])
        combo    = _state["combo_streak"]
        best_combo = _state["best_combo"]

    level_info = get_level_info()
    accuracy = round(correct / total * 100, 1) if total > 0 else 0.0

    try:
        from .trajectory_predictor import _sequence_store
        pattern_count = len(_sequence_store)
    except Exception:
        pattern_count = 0

    return {
        **level_info,
        "total_predictions":     total,
        "correct_predictions":   correct,
        "accuracy_pct":          accuracy,
        "agent_calls":           agent_calls,
        "vault_writes":          vw,
        "sessions_tracked":      sessions,
        "pattern_count":         pattern_count,
        "achievements_unlocked": len(achievements),
        "achievements_total":    len(ALL_ACHIEVEMENTS),
        "achievements":          achievements,
        "all_achievements":      ALL_ACHIEVEMENTS,
        "xp_log":                xp_log,
        "combo_streak":          combo,
        "best_combo":            best_combo,
        "combo_multiplier":      _combo_multiplier(combo),
    }


# ── Startup restore from PostgreSQL ──────────────────────────────────────────

def _restore_from_db() -> None:
    """Load persisted state on first import. Runs in a background thread."""
    try:
        from .intelligence_persistence import load_intelligence_state, start_background_save
        data = load_intelligence_state()
        if data:
            with _lock:
                for key in ("xp", "total_predictions", "correct_predictions",
                            "agent_calls", "vault_writes", "patterns_discovered",
                            "achievements", "xp_log", "combo_streak", "best_combo"):
                    if key in data:
                        _state[key] = data[key]
        start_background_save()
    except Exception:
        pass


import threading as _threading
_threading.Thread(target=_restore_from_db, daemon=True, name="iq-restore").start()
