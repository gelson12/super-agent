"""
Session trajectory predictor.

Tracks the sequence of agent types used per session (e.g. SHELL → GITHUB → SHELL)
and predicts the next likely agent by matching the current window against all
historically observed windows.

Zero API cost — pure in-memory counter matching, no LLM calls.
"""
import collections
import threading

_WINDOW = 3          # look-back depth for pattern matching
_MIN_CONFIDENCE = 0.5
_MAX_SEQUENCES = 1000  # bounded global store

# Global sequence store: list of (window_tuple, next_agent)
_sequence_store: list[tuple[tuple, str]] = []
_store_lock = threading.Lock()

# Per-session rolling history
_session_seqs: dict[str, list[str]] = {}
_seq_lock = threading.Lock()


def record_turn(session_id: str, agent_type: str) -> None:
    """
    Record one agent dispatch for a session.
    Must be called after every agent response so the predictor accumulates data.
    """
    with _seq_lock:
        seq = _session_seqs.setdefault(session_id, [])
        # Before appending, save the window → next_agent pair to global store
        if len(seq) >= 1:
            window = tuple(seq[-_WINDOW:])
            with _store_lock:
                _sequence_store.append((window, agent_type))
                if len(_sequence_store) > _MAX_SEQUENCES:
                    _sequence_store.pop(0)
        seq.append(agent_type)
        if len(seq) > 20:
            _session_seqs[session_id] = seq[-20:]


def predict_next(session_id: str) -> tuple[str | None, float]:
    """
    Return (predicted_next_agent, confidence) for this session.
    Uses the last _WINDOW turns as a lookup key against all observed sequences.
    Returns (None, 0.0) when there is insufficient history or no pattern match.
    """
    with _seq_lock:
        seq = list(_session_seqs.get(session_id, []))

    if not seq:
        return None, 0.0

    window_size = min(_WINDOW, len(seq))
    lookup = tuple(seq[-window_size:])

    with _store_lock:
        counts: dict[str, int] = collections.Counter()
        for stored_window, next_agent in _sequence_store:
            if stored_window[-window_size:] == lookup:
                counts[next_agent] += 1

    if not counts:
        return None, 0.0

    total = sum(counts.values())
    best_agent, best_count = counts.most_common(1)[0]
    confidence = best_count / total
    return (best_agent, confidence) if confidence >= _MIN_CONFIDENCE else (None, 0.0)


def get_session_sequence(session_id: str) -> list[str]:
    """Return the current agent sequence for a session (for observability)."""
    with _seq_lock:
        return list(_session_seqs.get(session_id, []))


# ── Startup restore from PostgreSQL ──────────────────────────────────────────

def _restore_from_db() -> None:
    """Reload sequence store from DB. Runs in a background thread."""
    try:
        from .intelligence_persistence import load_trajectory_state, start_background_save
        data = load_trajectory_state()
        if data:
            with _store_lock:
                for entry in data:
                    try:
                        window = tuple(entry[0])
                        next_agent = entry[1]
                        _sequence_store.append((window, next_agent))
                    except Exception:
                        pass
                if len(_sequence_store) > _MAX_SEQUENCES:
                    del _sequence_store[:-_MAX_SEQUENCES]
        start_background_save()
    except Exception:
        pass


import threading as _threading
_threading.Thread(target=_restore_from_db, daemon=True, name="traj-restore").start()
