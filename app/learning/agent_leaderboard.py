"""
Per-agent performance leaderboard.

Tracks 6 competitive metrics for each agent (N8N, SHELL, GITHUB, SELF_IMPROVE):
  calls           — total invocations (persisted across restarts)
  avg_ms          — average response time in milliseconds
  success_rate    — % of calls that didn't time out or crash
  pred_accuracy   — % of times this agent was correctly predicted
  unique_sessions — distinct sessions the agent was used in
  streak_best     — longest consecutive correct-prediction streak

  hot_agent       — which agent had the most calls in the last 60 minutes
                    (computed from hourly_calls timestamp list, not persisted)

Composite score (0–100):
  success_rate   × 0.30
  pred_accuracy  × 0.25
  speed score    × 0.20   (100 − avg_ms/1000, clamped 0–100)
  diversity      × 0.15   (capped at 20 sessions = 100%)
  streak         × 0.10   (capped at 10 = 100%)

State is restored from PostgreSQL on import and saved every 60 s
(via intelligence_persistence.start_background_save).
"""
import threading
import time

_AGENTS = ["N8N", "SHELL", "GITHUB", "SELF_IMPROVE"]

_AGENT_DISPLAY = {
    "N8N":          "N8N",
    "SHELL":        "SHELL",
    "GITHUB":       "GITHUB",
    "SELF_IMPROVE": "SELF-IMP",
}

_AGENT_COLORS = {
    "N8N":          "cyan",
    "SHELL":        "green",
    "GITHUB":       "purple",
    "SELF_IMPROVE": "orange",
}

_AGENT_ICONS = {
    "N8N":          "⚙️",
    "SHELL":        "💻",
    "GITHUB":       "🐙",
    "SELF_IMPROVE": "🔧",
}


def _blank() -> dict:
    return {
        "calls":          0,
        "total_ms":       0.0,
        "errors":         0,
        "sessions":       set(),
        "pred_correct":   0,
        "pred_total":     0,
        "streak_current": 0,
        "streak_best":    0,
        "last_call_ts":   0.0,
        # list of unix timestamps — one per call, used to compute last-hour activity
        "hourly_calls":   [],
    }


_stats: dict[str, dict] = {a: _blank() for a in _AGENTS}
_lock = threading.Lock()


def _normalise(key: str) -> str:
    return key.upper().replace("-", "_").replace(" ", "_")


# ── Public record functions ───────────────────────────────────────────────────

def record_call(agent_key: str, session_id: str, elapsed_ms: float, success: bool = True) -> None:
    """Record a completed agent call with timing and outcome."""
    key = _normalise(agent_key)
    if key not in _stats:
        return
    now = time.time()
    with _lock:
        s = _stats[key]
        s["calls"]        += 1
        s["total_ms"]     += elapsed_ms
        s["sessions"].add(session_id or "__unknown__")
        s["last_call_ts"]  = now
        s["hourly_calls"].append(now)
        # Prune timestamps older than 2 hours to keep the list bounded
        cutoff = now - 7200
        s["hourly_calls"] = [t for t in s["hourly_calls"] if t > cutoff]
        if not success:
            s["errors"] += 1

    # Notify daily challenges
    try:
        from .daily_challenges import record_agent_call as _dc_call
        _dc_call(key, elapsed_ms)
    except Exception:
        pass


def record_prediction_result(predicted_agent: str, actual_agent: str) -> None:
    """Track per-agent prediction accuracy and streak."""
    pred = _normalise(predicted_agent) if predicted_agent else None
    if not pred or pred not in _stats:
        return
    actual = _normalise(actual_agent) if actual_agent else ""
    with _lock:
        s = _stats[pred]
        s["pred_total"] += 1
        if pred == actual:
            s["pred_correct"]   += 1
            s["streak_current"] += 1
            if s["streak_current"] > s["streak_best"]:
                s["streak_best"] = s["streak_current"]
        else:
            s["streak_current"] = 0


# ── Hot Agent (last 60 minutes) ───────────────────────────────────────────────

def get_hot_agent() -> str | None:
    """
    Return the agent key with the most calls in the last 60 minutes, or None
    if there is no clear winner (tie or zero calls across all agents).
    """
    cutoff = time.time() - 3600
    with _lock:
        counts = {
            a: sum(1 for t in _stats[a]["hourly_calls"] if t > cutoff)
            for a in _AGENTS
        }
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    if not ranked or ranked[0][1] == 0:
        return None
    # Only crown a winner if they have at least 2× the second-place count
    if len(ranked) > 1 and ranked[1][1] > 0 and ranked[0][1] < ranked[1][1] * 2:
        return None
    return ranked[0][0]


# ── Leaderboard ───────────────────────────────────────────────────────────────

def get_leaderboard() -> list[dict]:
    """Return ranked list of per-agent stats, sorted by composite score."""
    hot = get_hot_agent()
    cutoff = time.time() - 3600

    with _lock:
        snapshot: dict[str, dict] = {}
        for a, s in _stats.items():
            d = dict(s)
            d["sessions"]     = len(s["sessions"])
            d["hourly_count"] = sum(1 for t in s["hourly_calls"] if t > cutoff)
            snapshot[a] = d

    rows = []
    for agent in _AGENTS:
        s = snapshot[agent]
        calls        = s["calls"]
        avg_ms       = round(s["total_ms"] / calls) if calls > 0 else 0
        success_rate = round((calls - s["errors"]) / calls * 100, 1) if calls > 0 else 0.0
        pred_acc     = (
            round(s["pred_correct"] / s["pred_total"] * 100, 1)
            if s["pred_total"] > 0 else 0.0
        )

        speed_score     = max(0.0, min(100.0, 100.0 - avg_ms / 1000.0))
        diversity_score = min(100.0, s["sessions"] / 20.0 * 100.0)
        streak_score    = min(100.0, s["streak_best"] / 10.0 * 100.0)

        composite = round(
            success_rate   * 0.30 +
            pred_acc       * 0.25 +
            speed_score    * 0.20 +
            diversity_score * 0.15 +
            streak_score   * 0.10,
            1,
        )

        rows.append({
            "agent":           agent,
            "display":         _AGENT_DISPLAY.get(agent, agent),
            "color":           _AGENT_COLORS.get(agent, "cyan"),
            "icon":            _AGENT_ICONS.get(agent, "🤖"),
            "calls":           calls,
            "avg_ms":          avg_ms,
            "success_rate":    success_rate,
            "pred_accuracy":   pred_acc,
            "pred_total":      s["pred_total"],
            "unique_sessions": s["sessions"],
            "streak_best":     s["streak_best"],
            "streak_current":  s["streak_current"],
            "hourly_count":    s["hourly_count"],
            "composite_score": composite,
            "last_call_ts":    s["last_call_ts"],
            "is_hot":          (agent == hot),
        })

    rows.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, row in enumerate(rows):
        row["rank"] = i + 1

    return rows


# ── Startup restore from PostgreSQL ──────────────────────────────────────────

def _restore_from_db() -> None:
    try:
        from .intelligence_persistence import load_leaderboard_state
        data = load_leaderboard_state()
        if not data:
            return
        with _lock:
            for agent, saved in data.items():
                if agent not in _stats:
                    continue
                s = _stats[agent]
                for key in ("calls", "total_ms", "errors", "sessions",
                            "pred_correct", "pred_total", "streak_current",
                            "streak_best", "last_call_ts"):
                    if key in saved:
                        s[key] = saved[key]
                # hourly_calls timestamps are not worth restoring after a restart
    except Exception:
        pass


import threading as _threading
_threading.Thread(target=_restore_from_db, daemon=True, name="lb-restore").start()
