"""
Agent/model status tracker — provides real-time state for the visual dashboard.

Each worker (model or agent) has a state:
  - "working"   — currently processing a request (set on entry, cleared on exit)
  - "idle"      — finished recently (< 30 min since last work)
  - "break"     — taking a short break (30 min to 3 hours since last work)
  - "idle"      — idle but available (3 to 6 hours since last work)
  - "sleeping"  — no work for 6+ hours
  - "talking"   — two workers collaborating (ensemble, peer review, CoT handoff)
  - "strike"    — Anthropic API credits insufficient ("salary insufficient")
  - "sick"      — CLI token invalid/expired (Claude CLI Pro or Gemini CLI)

Public API:
    mark_working(worker_id)        — called when a model/agent starts processing
    mark_done(worker_id)           — called when processing completes
    mark_talking(worker_a, worker_b) — called during multi-model collaboration
    clear_talking(worker_a, worker_b)
    mark_strike(worker_id)         — called when API credits run out
    mark_sick(worker_id)           — called when CLI token is invalid/expired
    get_all_statuses()             — returns dict of all workers with states
    get_worker_status(worker_id)   — returns single worker state
"""
import time
import threading

_lock = threading.Lock()

# worker_id → {"state": str, "last_worked": float, "talking_to": str|None, "task": str}
_workers: dict[str, dict] = {}

# Known workers — pre-populate so dashboard always shows all desks
_KNOWN_WORKERS = [
    # Models
    "Claude CLI Pro",
    "Gemini CLI",
    "Anthropic Haiku",
    "Sonnet Anthropic",
    "Opus Anthropic",
    "DeepSeek",
    # Agents
    "Shell Agent",
    "GitHub Agent",
    "N8N Agent",
    "Self-Improve Agent",
]

# Map from insight_log model names / routing sources → display worker IDs
_MODEL_MAP = {
    "CLI": "Claude CLI Pro",
    "GEMINI": "Gemini CLI",
    "GEMINI_CLI": "Gemini CLI",
    "HAIKU": "Anthropic Haiku",
    "CLAUDE": "Sonnet Anthropic",
    "SONNET": "Sonnet Anthropic",
    "OPUS": "Opus Anthropic",
    "DEEPSEEK": "DeepSeek",
    "SHELL": "Shell Agent",
    "GITHUB": "GitHub Agent",
    "N8N": "N8N Agent",
    "SELF_IMPROVE": "Self-Improve Agent",
    "ENSEMBLE": "Sonnet Anthropic",  # ensemble uses Claude as primary
}

_THIRTY_MIN = 30 * 60
_THREE_HOURS = 3 * 3600
_SIX_HOURS = 6 * 3600


def _ensure_worker(worker_id: str) -> dict:
    if worker_id not in _workers:
        _workers[worker_id] = {
            "state": "sleeping",
            "last_worked": 0,
            "talking_to": None,
            "task": "",
        }
    return _workers[worker_id]


def _init_known():
    with _lock:
        for w in _KNOWN_WORKERS:
            _ensure_worker(w)


_init_known()


def resolve_worker(model_or_route: str) -> str:
    """Map an insight_log model name to a dashboard worker ID."""
    return _MODEL_MAP.get(model_or_route.upper(), model_or_route)


def mark_working(worker_id: str, task: str = "") -> None:
    with _lock:
        w = _ensure_worker(worker_id)
        w["state"] = "working"
        w["task"] = task[:200]


def mark_done(worker_id: str) -> None:
    with _lock:
        w = _ensure_worker(worker_id)
        w["state"] = "idle"
        w["last_worked"] = time.time()
        w["task"] = ""
        w["talking_to"] = None


def mark_strike(worker_id: str) -> None:
    """Mark a worker as on strike — API credits insufficient."""
    with _lock:
        w = _ensure_worker(worker_id)
        w["state"] = "strike"
        w["task"] = "Salary insufficient"


def mark_sick(worker_id: str) -> None:
    """Mark a CLI worker as sick — token invalid/expired."""
    with _lock:
        w = _ensure_worker(worker_id)
        w["state"] = "sick"
        w["task"] = "Token invalid"


def mark_talking(worker_a: str, worker_b: str) -> None:
    with _lock:
        a = _ensure_worker(worker_a)
        b = _ensure_worker(worker_b)
        a["state"] = "talking"
        a["talking_to"] = worker_b
        b["state"] = "talking"
        b["talking_to"] = worker_a


def clear_talking(worker_a: str, worker_b: str) -> None:
    with _lock:
        a = _ensure_worker(worker_a)
        b = _ensure_worker(worker_b)
        if a["talking_to"] == worker_b:
            a["talking_to"] = None
            a["state"] = "idle"
            a["last_worked"] = time.time()
        if b["talking_to"] == worker_a:
            b["talking_to"] = None
            b["state"] = "idle"
            b["last_worked"] = time.time()


def get_worker_status(worker_id: str) -> dict:
    with _lock:
        w = _ensure_worker(worker_id)
        now = time.time()
        state = w["state"]

        # Auto-transition idle based on time since last work
        if state == "idle" and w["last_worked"] > 0:
            elapsed = now - w["last_worked"]
            if elapsed > _SIX_HOURS:
                state = "sleeping"
            elif elapsed > _THIRTY_MIN and elapsed <= _THREE_HOURS:
                state = "break"

        return {
            "id": worker_id,
            "state": state,
            "last_worked": w["last_worked"],
            "last_worked_ago": round(now - w["last_worked"]) if w["last_worked"] > 0 else None,
            "talking_to": w["talking_to"],
            "task": w["task"],
        }


def get_all_statuses() -> list[dict]:
    """Return status of all known workers, sorted: working first, then idle, then sleeping."""
    result = []
    for wid in _KNOWN_WORKERS:
        result.append(get_worker_status(wid))
    # Include any dynamically added workers not in _KNOWN_WORKERS
    with _lock:
        for wid in _workers:
            if wid not in _KNOWN_WORKERS:
                result.append(get_worker_status(wid))

    # Sort: working > talking > idle > sleeping
    order = {"working": 0, "talking": 1, "idle": 2, "sleeping": 3}
    result.sort(key=lambda x: order.get(x["state"], 4))
    return result


def seed_from_insight_log() -> None:
    """
    On startup, read the insight log to populate last_worked times
    so the dashboard doesn't show everything as sleeping.
    """
    try:
        from .insight_log import insight_log
        entries = insight_log._load_all()
        # Process chronologically so the latest timestamp wins
        for entry in entries:
            model = entry.get("model", "")
            worker_id = resolve_worker(model)
            ts = entry.get("ts", 0)
            with _lock:
                w = _ensure_worker(worker_id)
                if ts > w["last_worked"]:
                    w["last_worked"] = ts
                    w["state"] = "idle"
    except Exception:
        pass
