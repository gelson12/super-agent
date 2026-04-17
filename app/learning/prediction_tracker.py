"""
Prediction accuracy tracker.

Stores the prediction made after each agent response, then evaluates it
at the start of the next dispatch for that session.

Usage in dispatcher.py:
  After agent response:  store_prediction(session_id, predicted_agent)
  Before next dispatch:  evaluate_prediction(session_id, actual_agent)
"""
import threading

_pending: dict[str, str] = {}  # session_id → predicted_agent (uppercase)
_lock = threading.Lock()


def store_prediction(session_id: str, predicted_agent: str | None) -> None:
    if predicted_agent and session_id:
        with _lock:
            _pending[session_id] = predicted_agent.upper()


def evaluate_prediction(session_id: str, actual_agent: str) -> None:
    """
    Compare stored prediction for this session against the actual agent used.
    Records result in intelligence_score. No-ops if no pending prediction.
    """
    with _lock:
        predicted = _pending.pop(session_id, None)
    if not predicted:
        return
    was_correct = predicted == actual_agent.upper()
    try:
        from .intelligence_score import record_prediction
        record_prediction(was_correct)
    except Exception:
        pass
