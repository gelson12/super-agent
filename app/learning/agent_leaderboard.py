"""
Per-agent performance leaderboard.

Tracks 6 competitive metrics for each agent (N8N, SHELL, GITHUB, SELF_IMPROVE):
  calls           — total invocations
  avg_ms          — average response time in milliseconds
  success_rate    — % of calls that didn't time out or crash
  pred_accuracy   — % of times this agent was correctly predicted before being called
  unique_sessions — distinct sessions the agent was used in
  streak_best     — longest consecutive correct-prediction streak for this agent

Composite score (0–100):
  success_rate  × 0.30
  pred_accuracy × 0.25
  speed score   × 0.20   (100 − avg_ms/1000, clamped 0–100; 0 ms → 100, 100 s → 0)
  diversity     × 0.15   (capped at 20 unique sessions = 100%)
  streak        × 0.10   (capped at 10 streak = 100%)
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
        "calls":           0,
        "total_ms":        0.0,
        "errors":          0,
        "sessions":        set(),
        "pred_correct":    0,
        "pred_total":      0,
        "streak_current":  0,
        "streak_best":     0,
        "last_call_ts":    0.0,
    }


_stats: dict[str, dict] = {a: _blank() for a in _AGENTS}
_lock = threading.Lock()


def _normalise(key: str) -> str:
    return key.upper().replace("-", "_").replace(" ", "_")


def record_call(agent_key: str, session_id: str, elapsed_ms: float, success: bool = True) -> None:
    """Record a completed agent call with its timing and outcome."""
    key = _normalise(agent_key)
    if key not in _stats:
        return
    with _lock:
        s = _stats[key]
        s["calls"] += 1
        s["total_ms"] += elapsed_ms
        s["sessions"].add(session_id or "__unknown__")
        s["last_call_ts"] = time.time()
        if not success:
            s["errors"] += 1


def record_prediction_result(predicted_agent: str, actual_agent: str) -> None:
    """
    Track per-agent prediction accuracy.
    Call this alongside intelligence_score.record_prediction() in prediction_tracker.
    """
    pred = _normalise(predicted_agent) if predicted_agent else None
    if not pred or pred not in _stats:
        return
    actual = _normalise(actual_agent) if actual_agent else ""
    with _lock:
        s = _stats[pred]
        s["pred_total"] += 1
        if pred == actual:
            s["pred_correct"] += 1
            s["streak_current"] += 1
            if s["streak_current"] > s["streak_best"]:
                s["streak_best"] = s["streak_current"]
        else:
            s["streak_current"] = 0


def get_leaderboard() -> list[dict]:
    """Return ranked list of per-agent stats, sorted by composite score descending."""
    with _lock:
        snapshot = {}
        for a, s in _stats.items():
            d = dict(s)
            d["sessions"] = len(s["sessions"])
            snapshot[a] = d

    rows = []
    for agent in _AGENTS:
        s = snapshot[agent]
        calls = s["calls"]
        avg_ms = round(s["total_ms"] / calls) if calls > 0 else 0
        success_rate = round((calls - s["errors"]) / calls * 100, 1) if calls > 0 else 0.0
        pred_acc = (
            round(s["pred_correct"] / s["pred_total"] * 100, 1)
            if s["pred_total"] > 0 else 0.0
        )

        # Composite score components (each 0–100)
        speed_score      = max(0.0, min(100.0, 100.0 - avg_ms / 1000.0))
        diversity_score  = min(100.0, s["sessions"] / 20.0 * 100.0)
        streak_score     = min(100.0, s["streak_best"] / 10.0 * 100.0)

        composite = round(
            success_rate  * 0.30 +
            pred_acc      * 0.25 +
            speed_score   * 0.20 +
            diversity_score * 0.15 +
            streak_score  * 0.10,
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
            "composite_score": composite,
            "last_call_ts":    s["last_call_ts"],
        })

    rows.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, row in enumerate(rows):
        row["rank"] = i + 1

    return rows
