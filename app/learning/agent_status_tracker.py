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
  - "error"     — last request returned a structural error response (auto-clears on next success)

Public API:
    mark_working(worker_id)        — called when a model/agent starts processing
    mark_done(worker_id)           — called when processing completes
    mark_talking(worker_a, worker_b) — called during multi-model collaboration
    clear_talking(worker_a, worker_b)
    mark_strike(worker_id)         — called when API credits run out
    mark_sick(worker_id)           — called when CLI token is invalid/expired
    mark_error(worker_id, detail)  — called when a structural error response is returned
    get_all_statuses()             — returns dict of all workers with states
    get_worker_status(worker_id)   — returns single worker state
"""
import json
import os
import time
import threading
from pathlib import Path

_lock = threading.Lock()

# ── Per-agent persistent event log ────────────────────────────────────────────
_AGENT_LOG_BASE = (
    Path("/workspace/agent_logs")
    if os.access("/workspace", os.W_OK)
    else Path("./agent_logs")
)
_AGENT_LOG_MAX_LINES = 500
_AGENT_LOG_TRIM_TO   = 400

# ── PostgreSQL activity + interaction logging ──────────────────────────────────
_db_tables_ensured = False
_db_tables_lock    = threading.Lock()


def _get_db_conn():
    """Return a psycopg2 connection using DATABASE_URL, or None if unavailable."""
    try:
        import psycopg2
        url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
        if not url:
            return None
        return psycopg2.connect(url)
    except Exception:
        return None


def _ensure_db_tables() -> None:
    """Create agent_activity and agent_interactions tables if they don't exist."""
    global _db_tables_ensured
    if _db_tables_ensured:
        return
    with _db_tables_lock:
        if _db_tables_ensured:
            return
        conn = _get_db_conn()
        if not conn:
            return
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS agent_activity (
                            id          SERIAL PRIMARY KEY,
                            worker_id   TEXT        NOT NULL,
                            event       TEXT        NOT NULL,
                            detail      TEXT        DEFAULT '',
                            ts          DOUBLE PRECISION NOT NULL,
                            created_at  TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_agent_activity_worker
                            ON agent_activity(worker_id, ts DESC)
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS agent_interactions (
                            id          SERIAL PRIMARY KEY,
                            worker_a    TEXT        NOT NULL,
                            worker_b    TEXT        NOT NULL,
                            event       TEXT        NOT NULL,
                            detail      TEXT        DEFAULT '',
                            ts          DOUBLE PRECISION NOT NULL,
                            created_at  TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_agent_interactions_ts
                            ON agent_interactions(ts DESC)
                    """)
            _db_tables_ensured = True
        except Exception:
            pass
        finally:
            conn.close()


def _db_log_activity(worker_id: str, event: str, detail: str = "") -> None:
    """Write a single activity row to PostgreSQL (fire-and-forget in a daemon thread)."""
    def _write():
        _ensure_db_tables()
        conn = _get_db_conn()
        if not conn:
            return
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO agent_activity (worker_id, event, detail, ts) "
                        "VALUES (%s, %s, %s, %s)",
                        (worker_id, event, (detail or "")[:400], round(time.time(), 2)),
                    )
        except Exception:
            pass
        finally:
            conn.close()
    threading.Thread(target=_write, daemon=True).start()


def _db_log_interaction(worker_a: str, worker_b: str, event: str, detail: str = "") -> None:
    """Write a single interaction row to PostgreSQL (fire-and-forget in a daemon thread)."""
    def _write():
        _ensure_db_tables()
        conn = _get_db_conn()
        if not conn:
            return
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO agent_interactions "
                        "(worker_a, worker_b, event, detail, ts) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (worker_a, worker_b, event, (detail or "")[:400],
                         round(time.time(), 2)),
                    )
        except Exception:
            pass
        finally:
            conn.close()
    threading.Thread(target=_write, daemon=True).start()


def get_db_activity(worker_id: str, limit: int = 60) -> list[dict]:
    """Return the last `limit` activity rows for a worker from PostgreSQL."""
    _ensure_db_tables()
    conn = _get_db_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts, event, detail, created_at FROM agent_activity "
                "WHERE worker_id = %s ORDER BY ts DESC LIMIT %s",
                (worker_id, limit),
            )
            rows = cur.fetchall()
        return [
            {"ts": r[0], "event": r[1], "detail": r[2],
             "date": str(r[3])[:16] if r[3] else ""}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        conn.close()


def get_db_interactions(limit: int = 100) -> list[dict]:
    """Return the last `limit` agent interaction rows from PostgreSQL."""
    _ensure_db_tables()
    conn = _get_db_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts, worker_a, worker_b, event, detail, created_at "
                "FROM agent_interactions ORDER BY ts DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {"ts": r[0], "worker_a": r[1], "worker_b": r[2],
             "event": r[3], "detail": r[4],
             "date": str(r[5])[:16] if r[5] else ""}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        conn.close()


def _log_agent_event(worker_id: str, event: str, detail: str = "") -> None:
    """
    Append a timestamped event to:
      1. The per-agent JSONL log file (local, fast)
      2. The agent_activity PostgreSQL table (persistent, queryable)
    Called OUTSIDE _lock to avoid blocking state updates.
    """
    try:
        _AGENT_LOG_BASE.mkdir(parents=True, exist_ok=True)
        safe = worker_id.replace(" ", "_").replace("/", "_")
        log_file = _AGENT_LOG_BASE / f"{safe}.jsonl"

        entry = json.dumps({
            "ts": round(time.time(), 2),
            "event": event,
            "detail": (detail or "")[:200],
        })
        with log_file.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")

        # Trim periodically so files don't grow unbounded
        try:
            lines = log_file.read_text(encoding="utf-8").splitlines()
            if len(lines) > _AGENT_LOG_MAX_LINES:
                log_file.write_text(
                    "\n".join(lines[-_AGENT_LOG_TRIM_TO:]) + "\n",
                    encoding="utf-8",
                )
        except Exception:
            pass
    except Exception:
        pass

    # Also persist to PostgreSQL (non-blocking daemon thread)
    _db_log_activity(worker_id, event, detail)


def read_agent_log(worker_id: str, limit: int = 100) -> list[dict]:
    """Return the last `limit` events from a worker's log file, newest first."""
    try:
        base = (
            Path("/workspace/agent_logs")
            if Path("/workspace/agent_logs").exists()
            else Path("./agent_logs")
        )
        safe = worker_id.replace(" ", "_").replace("/", "_")
        log_file = base / f"{safe}.jsonl"
        if not log_file.exists():
            return []
        lines = log_file.read_text(encoding="utf-8").splitlines()
        events = []
        for line in reversed(lines[-limit * 2:]):  # read extra to filter
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                pass
            if len(events) >= limit:
                break
        return events  # already newest-first (reversed)
    except Exception:
        return []

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
    "SONNET_API": "Sonnet Anthropic",
    "OPUS": "Opus Anthropic",
    "DEEPSEEK": "DeepSeek",
    "SHELL": "Shell Agent",
    "GITHUB": "GitHub Agent",
    "N8N": "N8N Agent",
    "SELF_IMPROVE": "Self-Improve Agent",
    "ENSEMBLE": "Sonnet Anthropic",  # ensemble uses Claude as primary
}

# ── Worker-type classification ─────────────────────────────────────────────────
# API models: HTTP-based, always available when credentials valid.
# No idle-timeout — they don't go on break just because nobody asked them anything.
_API_MODELS  = {"Anthropic Haiku", "Sonnet Anthropic", "Opus Anthropic", "DeepSeek"}
# CLI workers: auth-dependent, token can expire — degrade over time.
_CLI_WORKERS = {"Claude CLI Pro", "Gemini CLI"}
# Agents: demand-triggered — moderate thresholds, no coffee_break.
_AGENTS      = {"Shell Agent", "GitHub Agent", "N8N Agent", "Self-Improve Agent"}

# CLI worker idle thresholds
_CLI_BREAK_AFTER    = 30 * 60    # 30 min  → break
_CLI_COFFEE_AFTER   = 2  * 3600  # 2 hours → coffee_break
_CLI_SLEEPING_AFTER = 8  * 3600  # 8 hours → sleeping

# Agent idle thresholds (no coffee_break)
_AGENT_BREAK_AFTER    = 30 * 60   # 30 min  → break
_AGENT_SLEEPING_AFTER = 2  * 3600 # 2 hours → sleeping

_SICK_GRACE_PERIOD = 15 * 60  # 15 min — show "recovering" before escalating to "sick"


def _ensure_worker(worker_id: str) -> dict:
    if worker_id not in _workers:
        _workers[worker_id] = {
            "state": "sleeping",
            "last_worked": 0,
            "talking_to": None,
            "task": "",
            "sick_since": None,        # timestamp when sick state was entered
            "error_detail": None,      # last error message, cleared on mark_done()
            "last_recovery_at": None,  # UTC float: when last recovery completed
            "last_recovery_layer": "", # which layer succeeded (e.g. "Playwright auto-login")
            "recovery_count_today": 0, # resets at UTC midnight
            "_recovery_day": 0,        # internal: UTC day number for daily reset
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
    _log_agent_event(worker_id, "working", task[:100])


def mark_done(worker_id: str) -> None:
    with _lock:
        w = _ensure_worker(worker_id)
        w["state"] = "idle"
        w["last_worked"] = time.time()
        w["task"] = ""
        w["talking_to"] = None
        w["sick_since"] = None   # clear sick grace period on recovery
        w["error_detail"] = None  # clear any error state on success
    _log_agent_event(worker_id, "done")


def mark_error(worker_id: str, detail: str = "") -> None:
    """Mark a worker as having returned a structural error response.
    This is distinct from sick/strike — the infrastructure is fine but the last
    response was an error token or failed task. Auto-clears on the next mark_done()."""
    with _lock:
        w = _ensure_worker(worker_id)
        w["state"] = "error"
        w["task"] = detail[:200] if detail else "Last response was an error"
        w["error_detail"] = detail[:400] if detail else ""
        w["last_worked"] = time.time()
    _log_agent_event(worker_id, "error", detail[:200])


def mark_strike(worker_id: str) -> None:
    """Mark a worker as on strike — API credits insufficient."""
    with _lock:
        w = _ensure_worker(worker_id)
        w["state"] = "strike"
        w["task"] = "Salary insufficient"
    _log_agent_event(worker_id, "strike", "No API credits — salary insufficient")


def mark_sick(worker_id: str) -> None:
    """Mark a CLI worker as sick — token invalid/expired.
    Dashboard shows 'break' for the first 15 min (self-healing window),
    then escalates to 'sick' (TOKEN ERR) only if recovery hasn't happened."""
    with _lock:
        w = _ensure_worker(worker_id)
        # Only stamp sick_since if it has never been set (or was explicitly cleared by
        # a CONFIRMED recovery via mark_done). Do NOT reset it just because state
        # briefly transitioned away from "sick" (e.g. a false-positive restore attempt
        # that set state="idle" before re-failing). This ensures the 15-min grace
        # period always counts from the ORIGINAL failure, not the last retry.
        if w.get("sick_since") is None:
            w["sick_since"] = time.time()
        w["state"] = "sick"
        w["task"] = "Recovering…"
    _log_agent_event(worker_id, "sick", "Token invalid or expired — self-healing active")


def record_recovery(worker_id: str, layer: str, duration_s: float) -> None:
    """Record a completed recovery event — updates last_recovery_at and daily count.
    Called from full_recovery_chain() and gemini_full_recovery() on success."""
    with _lock:
        w = _ensure_worker(worker_id)
        w["last_recovery_at"] = time.time()
        w["last_recovery_layer"] = layer
        today = int(time.time() // 86400)  # UTC day number
        if w.get("_recovery_day") != today:
            w["recovery_count_today"] = 0
            w["_recovery_day"] = today
        w["recovery_count_today"] = w.get("recovery_count_today", 0) + 1
    _log_agent_event(worker_id, "recovered",
                     f"Recovery via {layer} in {duration_s:.0f}s "
                     f"(#{w['recovery_count_today']} today)")


def mark_talking(worker_a: str, worker_b: str) -> None:
    with _lock:
        a = _ensure_worker(worker_a)
        b = _ensure_worker(worker_b)
        a["state"] = "talking"
        a["talking_to"] = worker_b
        b["state"] = "talking"
        b["talking_to"] = worker_a
    _log_agent_event(worker_a, "talking", f"Collaborating with {worker_b}")
    _log_agent_event(worker_b, "talking", f"Collaborating with {worker_a}")
    # Persist interaction to DB so history survives restarts
    _db_log_interaction(worker_a, worker_b, "started",
                        f"{worker_a} ↔ {worker_b} collaboration started")


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
    _log_agent_event(worker_a, "done_talking", f"Collaboration with {worker_b} ended")
    _log_agent_event(worker_b, "done_talking", f"Collaboration with {worker_a} ended")
    # Persist end-of-interaction to DB
    _db_log_interaction(worker_a, worker_b, "ended",
                        f"{worker_a} ↔ {worker_b} collaboration ended")


def get_worker_status(worker_id: str) -> dict:
    with _lock:
        w = _ensure_worker(worker_id)
        now = time.time()
        state = w["state"]

        # Sick grace period: show "recovering" for first 15 min, "sick" only after that.
        # This covers the self-healing window — distinct from idle-based break.
        if state == "sick":
            sick_since = w.get("sick_since")
            if sick_since and (now - sick_since) < _SICK_GRACE_PERIOD:
                state = "recovering"  # still within healing window

        # Worker-type-aware idle transitions:
        #   API models  → stay idle indefinitely (always available via HTTP)
        #   CLI workers → break@30m, coffee_break@2h, sleeping@8h
        #   Agents      → break@30m, sleeping@2h
        if state == "idle" and w["last_worked"] > 0:
            elapsed = now - w["last_worked"]
            if worker_id in _API_MODELS:
                pass  # API models never auto-degrade — always reachable
            elif worker_id in _CLI_WORKERS:
                if elapsed > _CLI_SLEEPING_AFTER:
                    state = "sleeping"
                elif elapsed > _CLI_COFFEE_AFTER:
                    state = "coffee_break"
                elif elapsed > _CLI_BREAK_AFTER:
                    state = "break"
            else:  # agents
                if elapsed > _AGENT_SLEEPING_AFTER:
                    state = "sleeping"
                elif elapsed > _AGENT_BREAK_AFTER:
                    state = "break"

        sick_since = w.get("sick_since")
        sick_for_seconds = round(now - sick_since) if sick_since else None
        return {
            "id": worker_id,
            "state": state,
            "last_worked": w["last_worked"],
            "last_worked_ago": round(now - w["last_worked"]) if w["last_worked"] > 0 else None,
            "talking_to": w["talking_to"],
            "task": w["task"],
            "sick_for_seconds": sick_for_seconds,          # how long truly sick (None if healthy)
            "error_detail": w.get("error_detail"),         # last error message (None if healthy)
            "last_recovery_at": w.get("last_recovery_at"), # UTC timestamp of last recovery
            "last_recovery_layer": w.get("last_recovery_layer", ""),
            "recovery_count_today": w.get("recovery_count_today", 0),
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

    # Sort: error/strike/sick first (need attention), then working, then idle/sleeping
    order = {"error": 0, "strike": 1, "sick": 2, "recovering": 3, "working": 4, "talking": 5, "idle": 6, "break": 7, "coffee_break": 8, "sleeping": 9}
    result.sort(key=lambda x: order.get(x["state"], 9))
    return result


def get_worker_history(worker_id: str, limit: int = 20) -> list[dict]:
    """
    Return recent activity history for a worker from the insight log.
    Each entry: {ts, date, model, routed_by, complexity, error, msg_preview}

    Uses both model name AND routed_by to map entries to workers:
    - Claude CLI Pro: model=CLAUDE with routed_by in {conversational, continuation, trivial}
      (these routes use CLI Pro via the dispatcher's main path)
    - Gemini CLI: model=GEMINI_CLI or routed_by contains 'gemini'
    - Specialized agents: matched by model name (SHELL, GITHUB, N8N, etc.)
    """
    try:
        from .insight_log import insight_log
        from datetime import datetime, timezone
        entries = insight_log._load_all()

        # Find all models that map to this worker
        matching_models = set()
        for model_key, wid in _MODEL_MAP.items():
            if wid == worker_id:
                matching_models.add(model_key)

        # Routes that indicate which worker handled the request
        _CLI_PRO_ROUTES = {
            "conversational", "continuation", "trivial", "trivial_cache",
            "forced", "forced_cache",
        }
        # Route prefixes → worker mapping (routed_by is often more reliable than model)
        _ROUTE_TO_WORKER = {
            "n8n_early": "N8N Agent",
            "n8n_keywords": "N8N Agent",
            "github_keywords": "GitHub Agent",
            "shell_keywords": "Shell Agent",
            "build_continuation": "Shell Agent",
            "self_improve": "Self-Improve Agent",
            "isolation_debug": "Shell Agent",
            "web_search": "Claude CLI Pro",
        }

        def _matches_worker(entry: dict) -> bool:
            model = entry.get("model", "").upper()
            route = entry.get("routed_by", "")

            # Direct model match (agents: SHELL, GITHUB, N8N, etc.)
            if model in matching_models:
                return True

            # Route-based matching (more reliable for agents)
            route_worker = _ROUTE_TO_WORKER.get(route)
            if route_worker == worker_id:
                return True

            # Claude CLI Pro: CLAUDE model via conversational routes
            if worker_id == "Claude CLI Pro":
                return model == "CLAUDE" and route in _CLI_PRO_ROUTES

            # Gemini CLI: explicit GEMINI_CLI model
            if worker_id == "Gemini CLI":
                return model in ("GEMINI_CLI", "GEMINI")

            return False

        history = []
        for entry in reversed(entries):
            if _matches_worker(entry):
                ts = entry.get("ts", 0)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
                history.append({
                    "ts": ts,
                    "date": dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "",
                    "model": entry.get("model", ""),
                    "routed_by": entry.get("routed_by", ""),
                    "complexity": entry.get("complexity", 0),
                    "error": entry.get("error", False),
                    "msg_words": entry.get("msg_words", 0),
                    "resp_len": entry.get("resp_len", 0),
                })
                if len(history) >= limit:
                    break
        return history
    except Exception:
        return []


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


def seed_live_status() -> None:
    """
    On startup, proactively check API credit status and CLI health
    so the dashboard reflects reality immediately (not just after first request).

    - Anthropic API: check if API key exists and has credits → strike if not
    - Claude CLI Pro: check if should_attempt_cli() → sick if not
    - Gemini CLI: check if available → sick if not
    """
    # Check Anthropic API credit status
    try:
        from ..config import settings
        if not settings.anthropic_api_key:
            mark_strike("Anthropic Haiku")
            mark_strike("Sonnet Anthropic")
            mark_strike("Opus Anthropic")
        else:
            # Try a minimal API call to check credits
            import anthropic
            try:
                client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "hi"}],
                )
                # If we get here, credits are available — leave as idle
            except Exception as e:
                err = str(e).lower()
                _no_credit = ("credit balance", "insufficient", "payment required", "no credits")
                if any(p in err for p in _no_credit):
                    mark_strike("Anthropic Haiku")
                    mark_strike("Sonnet Anthropic")
                    mark_strike("Opus Anthropic")
    except Exception:
        pass

    # Check Claude CLI Pro status — only mark sick for genuine failures,
    # NOT for transient rate limits (burst/daily flags)
    try:
        from .pro_router import is_cli_down
        if is_cli_down():
            mark_sick("Claude CLI Pro")
        # Also check boot-time sentinel written by entrypoint.cli.sh when the
        # CLAUDE_SESSION_TOKEN was detected as expired before supervisord started.
        # This ensures claude_pro tasks are routed to fallback from the very first
        # request instead of failing with 401 and triggering recovery reactively.
        import pathlib as _pl
        _sentinel = _pl.Path("/tmp/.claude_boot_sick")
        if _sentinel.exists():
            # Only apply if the worker has NOT already recovered since boot.
            # Once mark_done() runs (recovery success), sick_since is cleared to None
            # and state transitions away from sick — the sentinel is then stale.
            # Without this guard, the 30-min health check would re-mark sick every
            # cycle forever, producing false "recovering" badges even with a valid token.
            with _lock:
                _w = _workers.get("Claude CLI Pro", {})
                _already_recovered = (
                    _w.get("sick_since") is None
                    and _w.get("state") not in ("sick", "working")
                )
            if _already_recovered:
                # Worker healed since boot — sentinel is stale, remove it now
                try:
                    _sentinel.unlink(missing_ok=True)
                    _log_agent_event("Claude CLI Pro", "done",
                                     "Boot sentinel cleaned up — worker already recovered")
                except Exception:
                    pass
            else:
                mark_sick("Claude CLI Pro")
                _log_agent_event("Claude CLI Pro", "sick",
                                 "Boot sentinel: token expired or missing at startup")
        # ⚠️  Do NOT call mark_done("Claude CLI Pro") here.
        # is_cli_down() only checks whether a flag file has expired (10-min TTL) —
        # it does NOT verify that auth is actually valid.  Calling mark_done when the
        # flag has merely expired would:
        #   • set last_worked = now  (a lie — no real work happened)
        #   • clear sick_since       (resets the 15-min grace-period clock)
        # …causing the dashboard to loop forever in "short break" instead of
        # ever showing "sick".  Real recovery + mark_done is handled by:
        #   • pro_router.py / pro_cli_watchdog.py after verify_pro_auth() passes
        #   • cli_auto_login.py after successful Playwright login
    except Exception:
        pass

    # Check Gemini CLI status
    try:
        from .gemini_cli_worker import is_gemini_cli_available
        if not is_gemini_cli_available():
            mark_sick("Gemini CLI")
        # ⚠️  Same reasoning as Claude CLI Pro above — do NOT call mark_done here.
        # is_gemini_cli_available() is a quick binary probe, not an auth check.
        # mark_done("Gemini CLI") is called by gemini_cli_worker / gemini_token_keeper
        # only after a genuine successful response or confirmed credential restore.
    except Exception:
        pass
