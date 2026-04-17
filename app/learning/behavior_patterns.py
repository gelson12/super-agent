"""
Behavioral pattern learner.

Two independent predictors — both zero API cost:

1. Time predictor — tracks (weekday, hour) → {agent: count}.
   After seeing 5+ samples for a time bucket, predicts the most likely agent.
   "Monday mornings → deployment/shell" emerges naturally from usage.

2. Transition predictor — tracks agent_a → {agent_b: count}.
   After 3+ transitions, predicts what typically follows the current agent.
   "SHELL build → SHELL download_link" or "N8N activate → N8N check executions"
   surfaces without any hard-coded rules.

Both predictors persist entirely in-memory; they accumulate across requests
within a process lifetime and grow more accurate with each session.
"""
import collections
import datetime
import threading

# ── Time predictor ────────────────────────────────────────────────────────────
# bucket = (weekday 0-6, hour 0-23)
_time_counts: dict[tuple, dict[str, int]] = collections.defaultdict(
    lambda: collections.defaultdict(int)
)
_time_lock = threading.Lock()
_TIME_MIN_SAMPLES = 5

# ── Transition predictor ──────────────────────────────────────────────────────
_transitions: dict[str, dict[str, int]] = collections.defaultdict(
    lambda: collections.defaultdict(int)
)
_tr_lock = threading.Lock()
_last_agent: str | None = None
_last_lock = threading.Lock()
_TR_MIN_SAMPLES = 3


def record_dispatch(agent_type: str) -> None:
    """
    Record one agent dispatch event.
    Updates both the time predictor and the transition predictor.
    Must be called after every agent response.
    """
    global _last_agent

    now = datetime.datetime.utcnow()
    bucket = (now.weekday(), now.hour)

    with _time_lock:
        _time_counts[bucket][agent_type] += 1

    with _last_lock:
        prev = _last_agent
        _last_agent = agent_type

    if prev and prev != agent_type:
        with _tr_lock:
            _transitions[prev][agent_type] += 1


def predict_from_time() -> tuple[str | None, float]:
    """Predict next likely agent based on current UTC day/hour pattern."""
    now = datetime.datetime.utcnow()
    bucket = (now.weekday(), now.hour)

    with _time_lock:
        counts = dict(_time_counts.get(bucket, {}))

    if not counts or sum(counts.values()) < _TIME_MIN_SAMPLES:
        return None, 0.0

    total = sum(counts.values())
    best = max(counts, key=counts.get)
    confidence = counts[best] / total
    return (best, round(confidence, 2)) if confidence >= 0.5 else (None, 0.0)


def predict_after(agent_type: str) -> tuple[str | None, float]:
    """Predict what agent type typically follows `agent_type`."""
    with _tr_lock:
        counts = dict(_transitions.get(agent_type, {}))

    if not counts or sum(counts.values()) < _TR_MIN_SAMPLES:
        return None, 0.0

    total = sum(counts.values())
    best = max(counts, key=counts.get)
    confidence = counts[best] / total
    return (best, round(confidence, 2)) if confidence >= 0.5 else (None, 0.0)


def get_time_summary() -> str:
    """
    Human-readable summary of the time predictor for the current slot.
    Used in observability endpoints.
    """
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    now = datetime.datetime.utcnow()
    agent, conf = predict_from_time()
    if agent:
        return (
            f"{day_names[now.weekday()]} ~{now.hour:02d}h UTC → "
            f"{agent} ({int(conf*100)}% of past samples)"
        )
    return ""


def get_top_transitions() -> dict[str, tuple[str, float]]:
    """Return {from_agent: (to_agent, confidence)} for all known transitions."""
    result = {}
    for agent in list(_transitions.keys()):
        to_agent, conf = predict_after(agent)
        if to_agent:
            result[agent] = (to_agent, conf)
    return result
