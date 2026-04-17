import os as _os
import time as _time
from pathlib import Path as _Path

from .classifier import classify_request
from .preprocessor import detect_trivial, score_complexity, model_for_complexity
from ..models.claude import ask_claude, ask_claude_haiku
from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek
from ..agents.github_agent import run_github_agent
from ..agents.shell_agent import run_shell_agent
from ..agents.n8n_agent import run_n8n_agent
from ..agents.self_improve_agent import run_self_improve_agent
from ..security.safe_word import check_authorization
from ..cache.response_cache import cache
from ..learning.insight_log import insight_log
from ..learning.adapter import adapter
from ..learning.wisdom_store import wisdom_store
from ..learning.peer_review import peer_reviewer
from ..learning.ensemble import ensemble_voter
from ..learning.red_team import red_team
from ..learning.cot_handoff import cot_handoff
# Dashboard status tracker — all calls are best-effort, never crash dispatch
try:
    from ..learning.agent_status_tracker import (
        mark_working as _mark_working_raw, mark_done as _mark_done_raw,
        mark_talking as _mark_talking_raw, clear_talking as _clear_talking_raw,
        resolve_worker as _resolve_worker, mark_strike as _mark_strike_raw,
        mark_error as _mark_error_raw,
    )
    def _mark_working(w, t=""):
        try: _mark_working_raw(w, t)
        except Exception: pass
    def _mark_done(w):
        try: _mark_done_raw(w)
        except Exception: pass
    def _mark_talking(a, b):
        try: _mark_talking_raw(a, b)
        except Exception: pass
    def _clear_talking(a, b):
        try: _clear_talking_raw(a, b)
        except Exception: pass
    def _mark_strike(w):
        try: _mark_strike_raw(w)
        except Exception: pass
    def _mark_error(w, detail=""):
        try: _mark_error_raw(w, detail)
        except Exception: pass
except Exception:
    def _mark_working(w, t=""): pass
    def _mark_done(w): pass
    def _mark_talking(a, b): pass
    def _clear_talking(a, b): pass
    def _resolve_worker(m): return m
    def _mark_strike(w): pass
    def _mark_error(w, detail=""): pass
from ..memory.vector_memory import (
    get_memory_context, store_memory, store_enriched_memory,
    extract_and_store_insights,
)
from ..memory.session import get_compressed_context, append_exchange, get_session_history
from ..prompts import (
    build_capabilities_block,
    SYSTEM_PROMPT_CLAUDE,
    SYSTEM_PROMPT_HAIKU,
    get_prompt as _get_prompt,
)
from ..config import settings as _settings

# ── Predictive intelligence layer ─────────────────────────────────────────────
try:
    from ..learning.trajectory_predictor import (
        record_turn as _traj_record,
        predict_next as _traj_predict,
    )
except Exception:
    def _traj_record(s, a): pass
    def _traj_predict(s): return None, 0.0

try:
    from ..learning.behavior_patterns import (
        record_dispatch as _bp_record,
        predict_after as _bp_predict_after,
        predict_from_time as _bp_predict_time,
    )
except Exception:
    def _bp_record(a): pass
    def _bp_predict_after(a): return None, 0.0
    def _bp_predict_time(): return None, 0.0

try:
    from ..learning.context_prewarm import prewarm_for_agent as _prewarm
except Exception:
    def _prewarm(a): pass

# ── Active task tracker (per-session, in-memory) ─────────────────────────────
# Written before any long-running agent call so follow-up queries always know
# what was being worked on, even before append_exchange fires (which only
# happens after the agent COMPLETES — potentially 10+ minutes later).
# In-memory dict is faster, session-safe, and survives no disk permission issues.

import threading as _thr_active
_ACTIVE_TASK_TIMEOUT = 1200  # 20 minutes
_active_tasks: dict[str, tuple[float, str]] = {}  # session_id → (timestamp, task)
_active_tasks_lock = _thr_active.Lock()


def _write_active_task(session_id: str, task: str) -> None:
    with _active_tasks_lock:
        _active_tasks[session_id] = (_time.time(), task[:500])


def _read_active_task(session_id: str = "") -> tuple[bool, str, str]:
    """Returns (is_active, session_id, task_description)."""
    with _active_tasks_lock:
        if session_id:
            entry = _active_tasks.get(session_id)
            if entry:
                ts, task = entry
                if _time.time() - ts <= _ACTIVE_TASK_TIMEOUT:
                    return True, session_id, task
                del _active_tasks[session_id]
            return False, "", ""
        # No session_id: return any active task not yet expired
        for sid, (ts, task) in list(_active_tasks.items()):
            if _time.time() - ts <= _ACTIVE_TASK_TIMEOUT:
                return True, sid, task
            del _active_tasks[sid]
        return False, "", ""


def _clear_active_task(session_id: str = "") -> None:
    with _active_tasks_lock:
        if session_id:
            _active_tasks.pop(session_id, None)
        else:
            _active_tasks.clear()

_HANDLERS = {
    "GEMINI":   ask_gemini,
    "DEEPSEEK": ask_deepseek,
    "CLAUDE":   ask_claude,
    "HAIKU":    ask_claude_haiku,
    "GITHUB":   run_github_agent,
}

# ── Circuit breaker (in-memory, per-service) ──────────────────────────────────
# Prevents repeated calls to a service that is clearly down.
# Opens after _CB_THRESHOLD failures within _CB_WINDOW seconds.
_CB_FAILURES: dict[str, list[float]] = {}  # service → list of failure timestamps
_CB_WINDOW = 120    # 2-minute sliding window
_CB_THRESHOLD = 3   # open after 3 failures


def _cb_record_failure(service: str) -> None:
    """Record a failure timestamp for the given service."""
    now = _time.time()
    _CB_FAILURES.setdefault(service, [])
    _CB_FAILURES[service] = [t for t in _CB_FAILURES[service] if now - t < _CB_WINDOW]
    _CB_FAILURES[service].append(now)


def _cb_is_open(service: str) -> bool:
    """Return True if the circuit breaker is open (service appears down)."""
    now = _time.time()
    recent = [t for t in _CB_FAILURES.get(service, []) if now - t < _CB_WINDOW]
    return len(recent) >= _CB_THRESHOLD


def _cb_clear_failures(service: str) -> None:
    """Clear the circuit breaker failure window — called after a successful call."""
    _CB_FAILURES.pop(service, None)


# ── Per-session last-agent memory (in-process, best-effort) ──────────────────
# Tracks which operational agent handled the previous turn for each session.
# Used to re-route short follow-up replies back to the right agent when the
# message has no keywords (e.g. user answers "2" after the n8n agent listed options).
_session_last_agent: dict[str, str] = {}  # session_id → "N8N" | "SHELL" | "GITHUB" | "SELF_IMPROVE"
_SESSION_LAST_AGENT_TTL = 7200  # 2 hours — matches typical session length; was 30 min which was too short
_session_last_agent_ts: dict[str, float] = {}


def _set_last_agent(session_id: str, agent: str) -> None:
    """
    Record which operational agent handled the last turn for this session.
    Stored in-process dict (fast) AND persisted to the session store as a
    special marker message so it survives process restarts (Improvement B).
    """
    _session_last_agent[session_id] = agent
    _session_last_agent_ts[session_id] = _time.time()
    # Persist to DB as a system marker — prefixed so it's invisible to models
    # but readable by _get_last_agent on next process boot.
    try:
        from ..memory.session import get_session_history
        hist = get_session_history(f"__meta_{session_id}")
        hist.add_user_message(f"__last_agent__:{agent}:{_time.time()}")
    except Exception:
        pass  # never block on metadata write


def _get_last_agent(session_id: str) -> str | None:
    """
    Retrieve last operational agent for this session.
    Checks in-process dict first (fast path), then falls back to DB persistence
    so last_agent survives process restarts (Improvement B).
    """
    # Fast path: in-process dict
    if session_id in _session_last_agent:
        if _time.time() - _session_last_agent_ts.get(session_id, 0) > _SESSION_LAST_AGENT_TTL:
            _session_last_agent.pop(session_id, None)
        else:
            return _session_last_agent[session_id]
    # Slow path: DB fallback — only on cache miss (rare: first request after restart)
    try:
        from ..memory.session import get_session_history
        hist = get_session_history(f"__meta_{session_id}")
        msgs = hist.messages
        # Most recent __last_agent__ entry wins
        for m in reversed(msgs):
            c = getattr(m, "content", "")
            if c.startswith("__last_agent__:"):
                parts = c.split(":")
                if len(parts) >= 3:
                    agent_name = parts[1]
                    ts = float(parts[2])
                    if _time.time() - ts < _SESSION_LAST_AGENT_TTL:
                        # Warm the in-process cache
                        _session_last_agent[session_id] = agent_name
                        _session_last_agent_ts[session_id] = ts
                        return agent_name
                break
    except Exception:
        pass
    return None

# ── Proactive memory detection ────────────────────────────────────────────────

_SAVEABLE_PATTERNS: list[tuple[str, str, int]] = [
    # (pattern, memory_type, importance)
    # Decisions
    ("i decided to", "decision", 4),
    ("let's go with", "decision", 4),
    ("we'll use", "decision", 3),
    ("i chose", "decision", 4),
    ("the plan is to", "decision", 4),
    ("going forward we", "decision", 4),
    # Preferences
    ("i prefer", "preference", 3),
    ("always use", "preference", 3),
    ("never do", "preference", 3),
    ("i like it when", "preference", 3),
    ("don't ever", "preference", 4),
    # Project facts
    ("the repo is at", "fact", 4),
    ("the api key is", "fact", 5),
    ("the domain is", "fact", 4),
    ("the password is", "fact", 5),
    ("the url is", "fact", 4),
    ("we're using", "fact", 3),
    ("our stack is", "fact", 4),
    # Recurring problems
    ("this keeps happening", "problem", 4),
    ("again the same", "problem", 4),
    ("same issue as before", "problem", 4),
    ("recurring problem", "problem", 5),
    ("keeps failing", "problem", 4),
    # Goals
    ("i want to", "goal", 3),
    ("next we need to", "goal", 3),
    ("the goal is", "goal", 4),
    ("we need to finish", "goal", 4),
    ("by end of week", "goal", 4),
    ("the deadline is", "goal", 5),
]


def _detect_saveable_content(message: str, response: str) -> dict | None:
    """
    Detect if the user's message contains content worth enriching in memory.
    Returns {type, summary, importance} or None.
    """
    lower = message.lower()
    best_match = None
    best_importance = 0

    for pattern, mem_type, importance in _SAVEABLE_PATTERNS:
        if pattern in lower and importance > best_importance:
            best_match = (mem_type, importance)
            best_importance = importance

    if best_match is None:
        return None

    mem_type, importance = best_match
    # Build a concise summary from the message
    summary = message[:300]
    return {"type": mem_type, "summary": summary, "importance": importance}


_GITHUB_KEYWORDS = {
    "github", "repo", "repository", "repositories", "commit", "pull request",
    "open pr", "create pr", "branch", "list files in", "read file", "create file",
    "update file", "delete file", "push to repo", "merge branch",
    # Issue management
    "issue", "open issue", "close issue", "create issue", "comment on issue",
    "list issues", "github issue", "file an issue", "report bug",
    # Website / HTML modification triggers
    "website", "modify website", "update website", "change website", "edit website",
    "html", "index.html", "modify the", "change the link", "update the link",
    "instagram link", "instagram icon", "instagram url", "social link",
    "bridge-digital-solution", "bridge digital",
}

_SHELL_KEYWORDS = {
    # Email / calendar / secretary
    "email", "send email", "reply email", "forward email", "draft email",
    "inbox", "outlook", "calendar", "meeting", "schedule meeting",
    "secretary", "mark as read", "flag email", "delete email",
    "list emails", "search emails", "get email", "move email",
    "calendar event", "create event", "list events",
    # Shell / terminal
    "terminal", "shell", "run command", "execute", "workspace",
    "clone repo", "clone the repo", "list workspace", "ls /", "git clone",
    "fix the code", "auto fix", "autofix", "run the tests", "install package",
    "claude cli", "run claude",
    # Flutter / mobile build
    "flutter", "apk", "build apk", "build android", "build the app",
    "build app", "android app", "mobile app", "dart",
    # Voice app specific
    "voice app", "voice chat", "voice android", "build voice", "build and deliver",
    "speech to text", "flutter android", "install on android", "sideload",
    "download link", "apk download", "download apk", "install the app",
    "build it and package", "build and package",
}

_DEBUG_KEYWORDS = {
    "not working", "502", "503", "404", "error", "failing", "broken",
    "debug", "troubleshoot", "diagnose", "why is it", "why isn't",
    "service down", "can't connect", "cannot connect", "connection refused", "timeout",
    "root cause", "fix the issue", "what's wrong", "why is my",
    "unreachable", "can't reach", "cannot reach", "not reachable", "application not found",
    "instance unavailable", "not responding", "no response", "keeps failing",
}

_N8N_KEYWORDS = {
    # Direct n8n references
    "n8n", "workflow", "workflows", "automation", "trigger",
    "webhook", "execution", "executions",
    "create workflow", "update workflow", "activate workflow",
    "deactivate workflow", "run workflow", "debug workflow",
    "list workflows", "get workflow",
    # Scheduling / cron language
    "cron", "cron job", "cron expression", "scheduled task", "scheduled job",
    "run at", "run every", "every minute", "every hour", "every day", "every week",
    "every monday", "every night", "nightly", "daily job", "recurring task",
    "schedule this", "schedule it", "run on a schedule",
    # Natural language workflow building
    "make a workflow", "make me a workflow", "build a workflow",
    "build an automation", "create an automation", "create a automation",
    "set up a trigger", "set up an automation", "set up a workflow",
    "i want n8n", "use n8n", "automate this", "automate the",
    "send an email when", "notify me when", "when this happens",
    "schedule a task", "run every day", "run every hour", "run every week",
    "connect to slack", "post to slack", "send to slack",
    "pipe data", "sync data", "sync between", "build an integration",
    "make it automatic", "do it automatically", "do this automatically",
    "when someone", "whenever someone", "every time someone",
    "connect these", "link these", "integrate with",
}

_SELF_IMPROVE_KEYWORDS = {
    # Self-repair
    "fix yourself", "fix your", "heal yourself", "repair yourself",
    "auto fix", "autofix yourself", "self repair", "self-repair", "self heal",
    "auto repair", "auto-fix", "auto-heal",
    # Autonomous investigation triggers — user asking agent to act on its own
    "fix it yourself", "fix it", "can you not fix", "cant you fix",
    "not fix it", "investigate", "figure it out", "look into it",
    "resolve it", "sort it out", "handle it yourself", "do it yourself",
    "find out what", "find out why", "find the issue", "find the problem",
    "why is it", "what is wrong", "what went wrong", "find and fix",
    # Health / diagnosis
    "check your health", "health check", "how are you doing", "diagnose yourself",
    "what's failing", "what is failing", "show your errors", "show failures",
    "system health", "are you healthy", "check health",
    # Self-improvement
    "improve yourself", "improve your", "upgrade yourself", "update yourself",
    "make yourself better", "evolve", "learn from", "build algorithm",
    "build new algorithm", "create algorithm", "generate algorithm",
    # Infrastructure management
    "railway status", "railway logs", "redeploy", "deployment status",
    "check railway", "railway variables", "restart yourself",
    "check database", "db health", "session stats", "error stats",
    "failure patterns", "check cloudinary", "storage health",
    "check n8n", "n8n status", "check infrastructure", "infrastructure status",
    "check all services", "service status", "are all services",
    # Explicit self-modification
    "read your code", "read your source", "fix your code",
    "update your code", "modify yourself", "rewrite yourself",
    "patch yourself", "hotfix yourself",
}

_SEARCH_KEYWORDS = {
    "search", "look up", "look up", "google", "latest news", "what's happening",
    "current", "today", "what happened", "recent", "news about",
    "price of", "weather", "who is", "when did", "what is the score",
    "stock price", "live", "right now", "this week", "this year",
}

_CACHEABLE_MODELS = {"HAIKU", "GEMINI", "DEEPSEEK", "CLAUDE"}

# ── Routing confidence drift tracker ──────────────────────────────────────────
# Tracks a rolling window of classifier confidence scores per route.
# Drift in avg confidence signals the classifier is becoming misaligned with
# this user's language — surfaced via GET /metrics/routing-confidence.
import collections as _collections
_CONF_WINDOW = 50  # last N calls per route
_route_conf_history: dict[str, _collections.deque] = _collections.defaultdict(
    lambda: _collections.deque(maxlen=_CONF_WINDOW)
)


_conf_persist_counter = 0
_CONF_PERSIST_EVERY = 50  # snapshot to vault every N calls


def _record_routing_confidence(route: str, confidence: float) -> None:
    """Append one confidence observation for a route. Never raises."""
    global _conf_persist_counter
    try:
        if confidence > 0:
            _route_conf_history[route].append(round(confidence, 3))
        _conf_persist_counter += 1
        if _conf_persist_counter % _CONF_PERSIST_EVERY == 0:
            import threading as _thr_conf
            _thr_conf.Thread(target=_persist_routing_confidence_snapshot, daemon=True).start()
    except Exception:
        pass


def _persist_routing_confidence_snapshot() -> None:
    """Write a routing confidence snapshot to the vault. Daemon thread — never raises."""
    try:
        import datetime as _dt2
        _now = _dt2.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        _stats = get_routing_confidence_stats()
        if not _stats:
            return
        _lines = [f"\n## {_now} — routing confidence snapshot\n"]
        for r, s in _stats.items():
            _lines.append(
                f"- **{r}**: avg={s['avg_confidence']}, n={s['count']}, trend={s['trend']}\n"
            )
        _note = "".join(_lines)
        import asyncio as _aio2
        from mcp.client.sse import sse_client as _sse2
        from mcp import ClientSession as _CS2
        _URL2 = "http://obsidian-vault.railway.internal:22360/sse"

        async def _write():
            async with _sse2(url=_URL2) as (_r, _w):
                async with _CS2(_r, _w) as _s:
                    await _s.initialize()
                    await _s.call_tool("append_to_file",
                                       {"path": "KnowledgeBase/routing_confidence.md",
                                        "content": _note})

        _aio2.run(_write())
    except Exception:
        pass


def get_routing_confidence_stats() -> dict:
    """
    Return per-route avg confidence, sample count, and drift direction
    (positive slope = improving, negative = degrading).
    Exposed via GET /metrics/routing-confidence.
    """
    result = {}
    for route, hist in _route_conf_history.items():
        vals = list(hist)
        if not vals:
            continue
        avg = round(sum(vals) / len(vals), 3)
        # Simple slope: compare first half avg vs second half avg
        mid = len(vals) // 2
        if mid > 0:
            slope = round((sum(vals[mid:]) / len(vals[mid:]) - sum(vals[:mid]) / len(vals[:mid])), 3)
        else:
            slope = 0.0
        result[route] = {
            "avg_confidence": avg,
            "count": len(vals),
            "trend_slope": slope,
            "trend": "improving" if slope > 0.02 else "degrading" if slope < -0.02 else "stable",
        }
    return result

# ── Confidence scoring ────────────────────────────────────────────────────────

_HEDGE_WORDS = [
    "i think", "i believe", "i'm not sure", "i am not sure",
    "probably", "possibly", "might be", "may be", "not certain",
    "could be", "uncertain", "unclear", "i'm unsure", "perhaps",
    "it seems", "it appears", "roughly", "approximately",
]


def _score_confidence(response: str) -> int:
    """
    Scan response for hedge words → return confidence score 0–100.
    100 = fully confident, 0 = highly uncertain.
    Zero extra API calls — pure string scan.
    """
    lower = response.lower()
    hedge_count = sum(1 for h in _HEDGE_WORDS if h in lower)
    if hedge_count == 0:
        return 100
    if hedge_count == 1:
        return 80
    if hedge_count == 2:
        return 60
    if hedge_count == 3:
        return 40
    return max(0, 100 - hedge_count * 20)


# ── Keyword routing helpers ───────────────────────────────────────────────────

def _is_github_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _GITHUB_KEYWORDS)


def _is_shell_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _SHELL_KEYWORDS)


def _is_debug_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _DEBUG_KEYWORDS)


def _is_n8n_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _N8N_KEYWORDS)


_AGENT_TIMEOUTS: dict[str, int] = {
    "n8n_agent":          90,   # n8n API calls are fast; 90s is generous
    "github_agent":      300,   # GitHub API + potential file reads
    "shell_agent":       600,   # Flutter builds can take 5-8 min
    "self_improve_agent": 300,  # investigation + fix cycles
    "agent":             180,   # default fallback
}


def _vault_log_outcome(agent_type: str, message: str, response: str, session_id: str) -> None:
    """Log agent outcome to Obsidian vault — fire-and-forget, never raises."""
    try:
        from ..learning.vault_insight_hook import log_agent_outcome as _vlo
        _vlo(agent_type, message, response, session_id)
    except Exception:
        pass


# Next-step suggestion templates — keyed by (current_agent, predicted_next_agent).
# Zero API cost — pure string lookup.
_NEXT_STEP_HINTS: dict[tuple[str, str], str] = {
    ("SHELL",  "GITHUB"):       "Next: push the result to GitHub (`commit` or `create file` in the repo)?",
    ("SHELL",  "SHELL"):        "Next: grab the download link, or run another command?",
    ("SHELL",  "N8N"):          "Next: trigger an n8n automation with this output?",
    ("GITHUB", "SHELL"):        "Next: clone the repo and run it in the shell?",
    ("GITHUB", "GITHUB"):       "Next: open a PR, create a branch, or update another file?",
    ("GITHUB", "N8N"):          "Next: set up an n8n webhook to react to repo events?",
    ("N8N",    "N8N"):          "Next: activate the workflow, or check its execution history?",
    ("N8N",    "SHELL"):        "Next: run a shell command to verify the workflow's output?",
    ("N8N",    "GITHUB"):       "Next: commit the workflow JSON to GitHub for backup?",
    ("SELF_IMPROVE", "SHELL"):  "Next: verify the fix with a shell health check?",
    ("SELF_IMPROVE", "GITHUB"): "Next: push the fix to GitHub?",
}


def _maybe_add_next_step(response: str, session_id: str, current_agent: str) -> str:
    """
    Append a one-line predictive next-step hint to the response when the
    trajectory or behavioral predictor is confident enough.
    Zero extra API calls — uses in-memory pattern counters only.
    """
    try:
        # Prefer trajectory prediction (session-specific) over behavioral (global)
        next_agent, conf = _traj_predict(session_id)
        if not next_agent or conf < 0.6:
            next_agent, conf = _bp_predict_after(current_agent)
        if not next_agent or conf < 0.6:
            return response
        hint = _NEXT_STEP_HINTS.get((current_agent, next_agent))
        if hint:
            return response.rstrip() + f"\n\n> **Predicted next step** ({int(conf*100)}% likely): {hint}"
    except Exception:
        pass
    return response


def _record_and_prewarm(session_id: str, agent_type: str) -> None:
    """Record turn for all predictors and pre-warm vault for predicted next agent."""
    try:
        _traj_record(session_id, agent_type)
        _bp_record(agent_type)
        next_agent, conf = _traj_predict(session_id)
        if not next_agent or conf < 0.55:
            next_agent, _ = _bp_predict_after(agent_type)
        if next_agent:
            _prewarm(next_agent.lower())
    except Exception:
        pass


def _safe_agent_call(agent_fn, *args, agent_name: str = "agent", **kwargs) -> str:
    """Call an agent function with a per-agent timeout, catching any exception."""
    import concurrent.futures as _cf
    _timeout = _AGENT_TIMEOUTS.get(agent_name, _AGENT_TIMEOUTS["agent"])
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(agent_fn, *args, **kwargs)
            return _fut.result(timeout=_timeout)
    except _cf.TimeoutError:
        try:
            from ..activity_log import bg_log
            bg_log(f"{agent_name} timed out after {_timeout}s", source="dispatcher")
        except Exception:
            pass
        return f"[{agent_name} timed out after {_timeout}s — the operation took too long. Please try again or break the task into smaller steps.]"
    except Exception as e:
        err_msg = str(e)[:300].replace("\n", " ")
        try:
            from ..activity_log import bg_log
            bg_log(f"{agent_name} crashed: {err_msg}", source="dispatcher")
        except Exception:
            pass
        return f"[{agent_name} error: {err_msg}]"


def _is_self_improve_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _SELF_IMPROVE_KEYWORDS)


def _is_search_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _SEARCH_KEYWORDS)


def _parse_classify_result(result: str) -> tuple[str, float]:
    """Parse CATEGORY / CONFIDENCE lines from a classifier response."""
    category, confidence = "GENERAL", 0.0
    for line in result.splitlines():
        line = line.strip()
        if line.startswith("CATEGORY:"):
            category = line.split(":", 1)[1].strip().upper()
        elif line.startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return category, confidence


def _classify_route_with_confidence(message: str) -> tuple[str, float]:
    """
    Feature 5: Classify the route using a 3-tier CLI-first strategy.

    Tier 1 — Claude CLI Pro  (subscription, zero extra cost)
    Tier 2 — Gemini CLI      (free ~1 500 req/day)
    Tier 3 — Haiku API       (last resort — costs money)

    Falls back to ("GENERAL", 0.0) on total failure — never blocks dispatch.
    """
    prompt = (
        f'Classify this user message into exactly one category.\n'
        f'Message: "{message}"\n\n'
        f'Categories: SHELL, GITHUB, N8N, SELF_IMPROVE, SEARCH, GENERAL\n'
        f'Confidence: 0.0 to 1.0\n\n'
        f'Reply in exactly this format (two lines only, nothing else):\n'
        f'CATEGORY: <category>\n'
        f'CONFIDENCE: <0.0-1.0>'
    )

    # ── Tier 1: Claude CLI Pro ────────────────────────────────────────────────
    try:
        from ..learning.claude_code_worker import ask_claude_code
        result = ask_claude_code(prompt)
        if result and not result.startswith("["):
            category, confidence = _parse_classify_result(result)
            if category != "GENERAL" or confidence > 0.0:
                return category, confidence
    except Exception as _e1:
        try:
            from ..activity_log import bg_log as _bg_cl1
            _bg_cl1(f"Classifier Tier 1 (Claude CLI) error: {str(_e1)[:120]}", "classifier")
        except Exception:
            pass

    # ── Tier 2: Gemini CLI ────────────────────────────────────────────────────
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        result = ask_gemini_cli(prompt)
        if result and not result.startswith("["):
            category, confidence = _parse_classify_result(result)
            if category != "GENERAL" or confidence > 0.0:
                return category, confidence
    except Exception as _e2:
        try:
            from ..activity_log import bg_log as _bg_cl2
            _bg_cl2(f"Classifier Tier 2 (Gemini CLI) error: {str(_e2)[:120]}", "classifier")
        except Exception:
            pass

    # ── Tier 3: Haiku API (last resort) ──────────────────────────────────────
    try:
        result = ask_claude_haiku(prompt)
        return _parse_classify_result(result)
    except Exception as _e3:
        try:
            from ..activity_log import bg_log as _bg_cl3
            _bg_cl3(f"Classifier Tier 3 (Haiku API) error: {str(_e3)[:120]}", "classifier")
        except Exception:
            pass
        return "GENERAL", 0.0


# ── Extended result builder ───────────────────────────────────────────────────

def _build_extended_result(base: dict, **kwargs) -> dict:
    """
    Merge a base dispatch result dict with all new collective intelligence
    fields (with safe defaults). Keeps return shape consistent across all
    code paths (early returns, cache hits, agents, new layers).
    """
    return {
        **base,
        "was_reviewed": False,
        "critic_model": None,
        "critique_was_substantive": False,
        "is_ensemble": False,
        "models_used": [],
        "disagreement_detected": False,
        "cot_used": False,
        "reasoning_model": None,
        "answer_model": None,
        "trace_length": 0,
        "red_team_ran": False,
        "escalated": False,
        "red_verdict": None,
        "confidence_score": _score_confidence(base.get("response", "")),
        "cloudinary_url": None,
        "memory_count": 0,
        "routing_explanation": "",
        **kwargs,
    }


# ── Main dispatch function ────────────────────────────────────────────────────

_MAX_MESSAGE_LEN = 12_000


def dispatch(message: str, force_model: str | None = None, session_id: str = "default") -> dict:
    """
    Route a message through the full collective intelligence pipeline.

    Pipeline:
    1.  Safe-word guard
    2.  Forced model override
    3.  Trivial query bypass → Haiku directly
    4.  Complexity scoring
    4b. Isolation debug routing → SHELL agent with Isolate→Identify→Fix→Integrate stance
    5.  Keyword routing: SHELL → GITHUB
    6.  Adaptive model selection (Haiku ceiling from adapter)
    7.  Cache lookup
    8a. complexity == 5 → Ensemble (replaces single model call)
    8b. Single model call
        → CoT handoff (complexity >= 4, classifier-routed)
        → Peer review  (complexity >= 4)
        → Red team     (complexity >= 3, CONFIDENCE_MODE=true)
    9.  Cache write + insight log + adapter tick + wisdom record

    Returns dict with: model_used, response, routed_by, complexity, cache_hit,
    plus collective intelligence fields: was_reviewed, is_ensemble, cot_used,
    red_team_ran, escalated, confidence_score, cloudinary_url, and more.
    """

    # ── Daily briefing + weekly pattern promotion (fire-and-forget) ──────────
    try:
        from ..learning.daily_briefing import (
            trigger_daily_briefing_if_needed as _tbriefing,
            promote_patterns_if_needed as _tpromote,
        )
        _tbriefing()
        _tpromote()
    except Exception:
        pass

    # ── Message length guard ──────────────────────────────────────────────────
    if len(message) > _MAX_MESSAGE_LEN:
        return _build_extended_result({
            "model_used": "GUARD",
            "response": f"[Message too long — maximum {_MAX_MESSAGE_LEN:,} characters. Please shorten your request.]",
            "routed_by": "length_guard",
            "complexity": 0,
            "cache_hit": False,
        })

    # ── APP_CONTEXT: mobile app metadata routing (runs before everything else) ──
    # The mobile app may inject a [APP_CONTEXT]...[/APP_CONTEXT] block into the
    # message. If REQUEST_CATEGORY=LOCATION and ROUTE_TO=GEMINI_ONLY, the request
    # is handled exclusively by Gemini CLI — no classifier, no ensemble, no other model.
    try:
        from .app_context_parser import parse_app_context, is_location_request, build_location_prompt
        _app_ctx, _clean_msg = parse_app_context(message)
        if _app_ctx and is_location_request(_app_ctx):
            from ..learning.gemini_cli_worker import ask_gemini_cli
            _loc_prompt = build_location_prompt(_app_ctx)
            _response = ask_gemini_cli(_loc_prompt)
            _log(
                f"APP_CONTEXT routing: LOCATION → GEMINI_CLI "
                f"(lat={_app_ctx.get('CURRENT_LAT', '?')} "
                f"lon={_app_ctx.get('CURRENT_LON', '?')} "
                f"voice={_app_ctx.get('VOICE_MODE', 'false')})"
            )
            return _build_extended_result({
                "response": _response,
                "model_used": "GEMINI_CLI",
                "routed_by": "app_context:LOCATION:GEMINI_ONLY",
                "complexity": 3,
                "session_id": session_id,
            }, routing_explanation="Location request detected via APP_CONTEXT — routed directly to Gemini CLI.")
        # APP_CONTEXT present but not a location request — strip block, continue normally
        if _app_ctx:
            message = _clean_msg
    except Exception:
        pass  # Never block dispatch on parser failure — fall through to normal routing
    # ── End APP_CONTEXT ───────────────────────────────────────────────────────

    # Track wall-clock latency for the full dispatch — emitted in insight_log
    _dispatch_start = _time.time()
    _routing_confidence: float = 0.0  # filled in by classifier; used in insight_log

    # ── 0. Proactive cross-session memory injection ───────────────────────────
    # Always retrieve relevant past memories regardless of whether pgvector is up.
    # The JSON fallback in vector_memory.py ensures this always returns something
    # after the first few exchanges.
    memory_ctx = get_memory_context(message, session_id=session_id)
    _memory_count = memory_ctx.count("\n-") if memory_ctx else 0
    augmented_message = (memory_ctx + message) if memory_ctx else message

    # ── 0b. Session history injection ─────────────────────────────────────────
    # Prepend compressed conversation history so models have full in-session context
    _session_ctx = ""
    _ctx_injection_failed = False
    try:
        _session_ctx = get_compressed_context(session_id)
        if _session_ctx:
            augmented_message = (
                f"[Conversation history — this session]\n{_session_ctx}\n\n"
                f"[Current message]\n{augmented_message}"
            )
    except Exception as _session_err:
        _ctx_injection_failed = True
        from ..activity_log import bg_log as _bg_session
        _bg_session(
            f"Session context injection failed for session={session_id}: {_session_err}",
            source="dispatcher",
        )
        # Session history lost — escalate complexity so peer-review/ensemble compensates

    if _ctx_injection_failed:
        augmented_message = (
            augmented_message
            + "\n[⚠️ Note: Session history is temporarily unavailable — context from earlier in this conversation may be missing.]"
        )

    # ── 0c-extra. Continuation detection ─────────────────────────────────────
    # Short follow-up messages ("nothing", "you didn't give", "fix that", "try again")
    # have no keywords and get mis-routed to web_search.
    # If the session has recent history AND the message is short + complaint-like,
    # inject full session context and route to Claude directly rather than searching.
    #
    # TIGHTENED RULES (was too generic — "still", "what about", "so what" matched
    # unrelated new questions): require EITHER a precise pattern OR 2+ loose patterns.
    _CONTINUATION_PRECISE = (
        # Explicit complaints about the previous response
        "you didn't", "you did not", "you forgot", "you missed", "you stopped",
        "you didn't finish", "it failed", "it didn't work", "it did not work",
        "no link", "no download", "no url", "fix that", "try again", "retry",
        # Explicit follow-up intents with no ambiguity
        "where is the link", "where's the link", "download link", "what's the url",
        "still building", "still working", "are you done", "did you finish",
        "is it ready", "keep going", "next step", "go on", "proceed",
    )
    _CONTINUATION_LOOSE = (
        # Ambiguous — only count if 2+ match
        "still", "what about", "and the", "but you", "you said", "as i said",
        "i said", "what phase", "which phase", "where is", "what happened",
        "whats happening", "what's happening", "any update", "status", "progress",
        "continue", "didn't", "did not", "nothing", "wrong", "incorrect",
        "broken", "missing", "incomplete", "not working", "failed",
        "so what", "the link",
    )
    _active_task_exists, _active_task_sid, _active_task_desc = _read_active_task()
    _msg_lower_cont = message.lower()
    _precise_match = any(p in _msg_lower_cont for p in _CONTINUATION_PRECISE)
    _loose_count = sum(1 for p in _CONTINUATION_LOOSE if p in _msg_lower_cont)
    _is_short_followup = (
        len(message.split()) <= 25
        and (_session_ctx or _active_task_exists)
        and (_precise_match or _loose_count >= 2)
    )

    # If an active task is running but session history isn't stored yet
    # (append_exchange fires only after completion), inject the task description
    # so follow-up messages always have context about what's being worked on.
    if _active_task_exists and not _session_ctx:
        augmented_message = (
            f"[ACTIVE TASK — Super Agent is currently working on this]\n"
            f"{_active_task_desc}\n\n"
            f"[Current message]\n{augmented_message}"
        )

    # ── 0c. Build capabilities-aware system prompts ───────────────────────────
    _caps = build_capabilities_block(_settings)
    _learned = adapter.get_learned_context() or ""
    _raw_claude = _get_prompt("system_claude") or SYSTEM_PROMPT_CLAUDE
    _raw_haiku = _get_prompt("system_haiku") or SYSTEM_PROMPT_HAIKU
    # Use .replace() instead of .format() so that {curly braces} inside
    # stored memory (e.g. n8n JSON) don't cause a KeyError in .format().
    _system_claude = _raw_claude.replace("{capabilities}", _caps).replace("{learned_context}", _learned)
    _system_haiku  = _raw_haiku.replace("{capabilities}", _caps).replace("{learned_context}", _learned)

    # ── 0d. Vault context injection ───────────────────────────────────────────
    # Multi-source structured injection for all 4 agents and API models.
    # Sources: agent patterns file (cached 30 min) + query search + errors.md (if error
    # keywords) + recent activity. Returns a [VAULT CONTEXT] block with ### sections.
    # Session-aware dedup: on follow-up turns (_session_ctx non-empty) the patterns file
    # is already in session history — skip re-fetching, only do query search.
    _vault_ctx = ""
    _is_clear_agent_route = (
        _is_n8n_request(message) or _is_shell_request(message)
        or _is_github_request(message) or _is_self_improve_request(message)
    )
    # Determine agent type label for patterns file lookup
    if _is_n8n_request(message):            _agent_type_key = "n8n"
    elif _is_shell_request(message):        _agent_type_key = "shell"
    elif _is_github_request(message):       _agent_type_key = "github"
    elif _is_self_improve_request(message): _agent_type_key = "self_improve"
    else:                                   _agent_type_key = ""

    # Session dedup: patterns already in context on follow-up turns
    _is_first_session_turn = not bool(_session_ctx)
    _include_patterns = _is_first_session_turn  # re-inject only on turn 1

    if _cb_is_open("vault"):
        _system_claude = _system_claude.replace("{vault_context}", "")
        _system_haiku  = _system_haiku.replace("{vault_context}",  "")
    else:
        try:
            from ..prompts import get_vault_context_block as _get_vault
            # Build smarter search hint — extract key nouns rather than raw message prefix
            _stop_words = {
                "can", "you", "please", "the", "a", "an", "i", "me", "my", "is", "are",
                "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
                "did", "will", "would", "could", "should", "may", "might", "to", "of",
                "in", "on", "at", "for", "with", "this", "that", "it", "its", "and",
                "or", "but", "so", "if", "not", "no", "yes", "how", "what", "why",
                "when", "where", "who", "which", "there", "then", "just", "also",
            }
            _words = [w.strip(".,!?:;'\"") for w in message.split() if len(w) > 3]
            _key_words = [w for w in _words if w.lower() not in _stop_words][:6]
            _key_phrase = " ".join(_key_words) if _key_words else message[:60]
            if _is_n8n_request(message):
                _vault_hint = f"n8n workflow {_key_phrase}"
            elif _is_shell_request(message):
                _vault_hint = f"shell railway {_key_phrase}"
            elif _is_github_request(message):
                _vault_hint = f"github {_key_phrase}"
            elif _is_self_improve_request(message):
                _vault_hint = f"infrastructure fix {_key_phrase}"
            else:
                _vault_hint = _key_phrase if len(message) > 20 else ""
            _vault_ctx = _get_vault(
                topic_hint=_vault_hint,
                agent_type=_agent_type_key,
                include_patterns=_include_patterns,
            )
            _system_claude = _system_claude.replace("{vault_context}", _vault_ctx)
            _system_haiku  = _system_haiku.replace("{vault_context}",  _vault_ctx)
        except Exception:
            _cb_record_failure("vault")
            _system_claude = _system_claude.replace("{vault_context}", "")
            _system_haiku  = _system_haiku.replace("{vault_context}",  "")

    # Build agent-augmented message. Higher cap for agent routes.
    _vault_prefix_cap = 2500 if _is_clear_agent_route else 1500
    _vault_prefix = _vault_ctx[:_vault_prefix_cap] if _vault_ctx else ""
    _agent_message = (
        f"{_vault_prefix}\n{augmented_message}" if _vault_prefix else augmented_message
    )

    # ── 0d-extra. Today's briefing injection (first session turn only) ────────
    # If self_improve wrote today's briefing, prepend it so agents start with
    # shared situational awareness about what happened earlier today.
    if _is_first_session_turn and _is_clear_agent_route:
        try:
            from ..prompts import get_todays_briefing as _get_briefing
            _briefing = _get_briefing()
            if _briefing:
                _agent_message = (
                    f"[TODAY'S AGENT BRIEFING]\n{_briefing[:600]}\n\n{_agent_message}"
                )
        except Exception:
            pass

    # ── 0e-extra. Session goal injection ─────────────────────────────────────
    # On the first turn of a new session, extract the user's likely goal via Haiku
    # and store it so every subsequent agent turn has goal context.
    # On follow-up turns, retrieve the stored goal and prepend it.
    _session_goal = ""
    try:
        _goal_key = f"__goal_{session_id}"
        _goal_hist = get_session_history(_goal_key)
        _existing_goals = _goal_hist.messages
        if _existing_goals:
            # Retrieve stored goal
            _session_goal = _existing_goals[-1].content
        elif _session_ctx == "" and len(message.split()) >= 5:
            # First message of session — extract goal in background thread
            import threading as _thr_goal
            def _extract_goal():
                try:
                    from ..models.claude import ask_claude_haiku as _haiku
                    _g = _haiku(
                        f"In one sentence (max 20 words), what is the user trying to accomplish?\n\nMessage: {message[:400]}",
                        system="Output only the goal sentence, no preamble."
                    )
                    _g = _g.strip()
                    if _g and len(_g) > 10:
                        _goal_hist.add_ai_message(_g)
                except Exception:
                    pass
            _thr_goal.Thread(target=_extract_goal, daemon=True).start()
    except Exception:
        pass

    if _session_goal:
        _agent_message = (
            f"[Session goal: {_session_goal}]\n{_agent_message}"
        )
        augmented_message = (
            f"[Session goal: {_session_goal}]\n{augmented_message}"
        )

    # ── 0e-2. Behavioral time prediction — inject as context hint ────────────
    # If the time-based predictor is confident, prepend a brief hint so the
    # dispatcher and agents are aware of what Gelson typically needs at this hour.
    _time_agent_hint, _time_agent_conf = _bp_predict_time()
    if _time_agent_hint and _time_agent_conf >= 0.65:
        _time_hint_str = (
            f"[Behavioral pattern: at this time you typically use the "
            f"{_time_agent_hint} agent — context pre-loaded]\n"
        )
        _agent_message = _time_hint_str + _agent_message

    # ── 0f. Proactive anomaly surface ─────────────────────────────────────────
    # If a critical anomaly was detected within the last 5 minutes by the anomaly
    # alerter, prepend a brief notice to the response so the user sees it in-band.
    _pending_anomaly = ""
    try:
        from ..learning.anomaly_alerter import get_recent_alert as _get_anomaly
        _pending_anomaly = _get_anomaly(max_age_s=300) or ""
    except Exception:
        pass

    # ── 0e. Context-loss escalation ───────────────────────────────────────────
    # If session history injection failed, we're flying blind — escalate complexity
    # so peer-review or ensemble compensates for missing context.
    if _ctx_injection_failed:
        complexity = score_complexity(message)
        complexity = max(complexity, 3)  # at minimum Sonnet, not Haiku

    # ── 0g. Compound query decomposition ─────────────────────────────────────
    # Detect messages with multiple independent requests separated by "AND", "ALSO",
    # "THEN", "+" — fan each sub-request to the right agent and merge responses.
    # Only applies when: no force_model, message > 15 words, at least one separator
    # maps to two DIFFERENT agents. Falls through silently on any failure.
    _COMPOUND_SPLITS = (" and also ", " and then ", " then ", " also ", " plus ")
    _msg_norm = " " + message.lower() + " "
    # Only decompose if exactly one separator present (prevents runaway fan-out on
    # messages like "do A and also B and also C and also D")
    _split_on = next(
        (s for s in _COMPOUND_SPLITS if 1 <= _msg_norm.count(s) <= 2 and len(message.split()) > 15),
        None,
    )
    if _split_on and not force_model:
        try:
            _sep = _split_on.strip()
            # Case-insensitive split on first occurrence
            import re as _re_compound
            _parts = _re_compound.split(rf'\b{_re_compound.escape(_sep)}\b', message, maxsplit=1, flags=_re_compound.IGNORECASE)
            if len(_parts) == 2:
                _p1, _p2 = _parts[0].strip(), _parts[1].strip()
                def _quick_route(txt: str) -> str:
                    if _is_n8n_request(txt):     return "N8N"
                    if _is_shell_request(txt):   return "SHELL"
                    if _is_github_request(txt):  return "GITHUB"
                    if _is_self_improve_request(txt): return "SELF_IMPROVE"
                    return "GENERAL"
                _r1 = _quick_route(_p1)
                _r2 = _quick_route(_p2)
                # Only decompose if both parts are substantive and map to different agents
                _AGENT_ROUTES_SET = {"N8N", "SHELL", "GITHUB", "SELF_IMPROVE"}
                if (
                    len(_p1.split()) >= 4 and len(_p2.split()) >= 4
                    and _r1 in _AGENT_ROUTES_SET and _r2 in _AGENT_ROUTES_SET
                    and _r1 != _r2
                ):
                    import threading as _thr_c
                    _sub_responses: dict = {}
                    def _run_sub(part, route, key):
                        try:
                            _sub_responses[key] = dispatch(part, session_id=session_id)
                        except Exception as _e:
                            _sub_responses[key] = {"response": f"[Sub-task failed: {_e}]", "model_used": route}
                    _t1 = _thr_c.Thread(target=_run_sub, args=(_p1, _r1, "a"), daemon=True)
                    _t2 = _thr_c.Thread(target=_run_sub, args=(_p2, _r2, "b"), daemon=True)
                    _t1.start(); _t2.start()
                    _t1.join(timeout=120); _t2.join(timeout=120)
                    if "a" in _sub_responses and "b" in _sub_responses:
                        _resp_a = _sub_responses["a"].get("response", "")
                        _resp_b = _sub_responses["b"].get("response", "")
                        _combined = f"**Part 1** ({_r1}):\n{_resp_a}\n\n---\n\n**Part 2** ({_r2}):\n{_resp_b}"
                        return _build_extended_result({
                            "model_used": f"{_r1}+{_r2}",
                            "response": _combined,
                            "routed_by": "compound_decomposed",
                            "complexity": max(
                                _sub_responses["a"].get("complexity", 2),
                                _sub_responses["b"].get("complexity", 2),
                            ),
                            "cache_hit": False,
                        }, memory_count=_memory_count,
                        routing_explanation=f"Compound query decomposed into two sub-tasks ({_r1} + {_r2}) and executed in parallel.")
        except Exception:
            pass  # Fall through to normal routing on any failure

    # ── 1. Safe word guard ────────────────────────────────────────────────────
    authorized, block_reason = check_authorization(message)
    if not authorized:
        return _build_extended_result({
            "model_used": "SECURITY",
            "response": block_reason,
            "routed_by": "safe_word_guard",
            "complexity": 0,
            "cache_hit": False,
        }, routing_explanation="Request blocked by safe-word guard — write operation requires owner authorization.")

    # ── 2. Forced model override ──────────────────────────────────────────────
    if force_model:
        model = force_model.upper()
        if model == "SHELL":
            _write_active_task(session_id, message)
            response = run_shell_agent(message, authorized=authorized)
            _clear_active_task()
            insight_log.record(message, "SHELL", response, "forced", 3, session_id)
            adapter.tick()
            return _build_extended_result({
                "model_used": "SHELL",
                "response": response,
                "routed_by": "forced",
                "complexity": 3,
                "cache_hit": False,
            }, routing_explanation="Shell agent forced by caller via force_model override.")
        if model not in _HANDLERS:
            return _build_extended_result({
                "model_used": None,
                "response": (
                    f"Unknown model '{force_model}'. "
                    "Choose GEMINI, DEEPSEEK, CLAUDE, HAIKU, GITHUB, or SHELL."
                ),
                "routed_by": "forced",
                "complexity": 0,
                "cache_hit": False,
            }, routing_explanation=f"Invalid force_model value '{force_model}' — request rejected.")
        complexity = score_complexity(message)
        cached = cache.get(message, model) if model in _CACHEABLE_MODELS else None
        if cached:
            insight_log.record(message, model, cached, "forced_cache", complexity, session_id)
            adapter.tick()
            return _build_extended_result({
                "model_used": model,
                "response": cached,
                "routed_by": "forced_cache",
                "complexity": complexity,
                "cache_hit": True,
            }, routing_explanation="Cache hit on forced-model request — served from response cache.")
        response = _HANDLERS[model](message)
        if model in _CACHEABLE_MODELS:
            cache.set(message, model, response)
        insight_log.record(message, model, response, "forced", complexity, session_id)
        adapter.tick()
        wisdom_store.record_outcome(model, wisdom_store._detect_category("forced", model), response.startswith("["))
        return _build_extended_result({
            "model_used": model,
            "response": response,
            "routed_by": "forced",
            "complexity": complexity,
            "cache_hit": False,
        }, routing_explanation=f"Model {model} forced by caller via force_model override (complexity={complexity}).")

    # ── 3. Trivial bypass ─────────────────────────────────────────────────────
    if detect_trivial(message):
        cached = cache.get(message, "HAIKU")
        if cached:
            insight_log.record(message, "HAIKU", cached, "trivial_cache", 1, session_id)
            adapter.tick()
            return _build_extended_result({
                "model_used": "HAIKU",
                "response": cached,
                "routed_by": "trivial_cache",
                "complexity": 1,
                "cache_hit": True,
            }, routing_explanation="Trivial query — cache hit, served from Haiku response cache.")
        response = ask_claude_haiku(message, system=_system_haiku)
        cache.set(message, "HAIKU", response)
        insight_log.record(message, "HAIKU", response, "trivial", 1, session_id)
        adapter.tick()
        wisdom_store.record_outcome("HAIKU", "trivial/chat", response.startswith("["))
        # Always store even trivial exchanges — they build up user context over time
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        return _build_extended_result({
            "model_used": "HAIKU",
            "response": response,
            "routed_by": "trivial",
            "complexity": 1,
            "cache_hit": False,
        }, memory_count=_memory_count)

    # ── 3b-pre. Agent follow-up re-routing ───────────────────────────────────
    # When an operational agent (N8N / SHELL / SELF_IMPROVE / GITHUB) asked the
    # user a clarifying question and the user replies with a short answer (e.g.
    # "2", "option 1", "delete the old one"), the dispatcher has no keywords and
    # would otherwise mis-route to CLAUDE (conversational). Instead, route back
    # to the same agent that asked the question so it has the full context.
    _last_op_agent = _get_last_agent(session_id)
    _AGENT_CHOICE_PATTERNS = (
        r"^\s*\d+\s*[.):]",          # starts with a digit: "2.", "2:", "2)"
        r"^\s*(option|choice)\s+\d",  # "option 2", "choice 1"
        r"^\s*(go with|use|pick|choose|select)\s+(option\s+)?\d",  # "go with 2"
    )
    import re as _re
    _looks_like_agent_reply = (
        _last_op_agent is not None
        and len(message.split()) <= 30
        and _session_ctx  # must have prior conversation in session
        and (
            any(_re.match(p, message.strip(), _re.IGNORECASE) for p in _AGENT_CHOICE_PATTERNS)
            or len(message.split()) <= 10  # very short message after an operational turn
        )
    )
    if _looks_like_agent_reply and not (
        _is_n8n_request(message) or _is_shell_request(message)
        or _is_github_request(message) or _is_self_improve_request(message)
    ):
        # BUG 1 FIX: rebuild augmented_message specifically for the follow-up.
        # The outer augmented_message was built before we knew this was a follow-up
        # and may have a stale/failed session ctx. Rebuild it with the explicit label
        # "follow-up reply" so the agent understands the conversational context.
        _followup_base = (
            f"[Conversation history — this session]\n{_session_ctx}\n\n"
            f"[User's follow-up reply to your previous message]\n{memory_ctx}{message}"
            if _session_ctx
            else augmented_message  # best we have if context injection failed
        )
        _followup_aug = (
            f"{_vault_ctx}\n{_followup_base}" if _vault_ctx else _followup_base
        )

        if _last_op_agent == "N8N":
            from ..agents.n8n_agent import run_n8n_agent
            _write_active_task(session_id, message)
            response = _safe_agent_call(run_n8n_agent, _followup_aug, agent_name="n8n_agent")
            _clear_active_task()
            insight_log.record(message, "N8N", response, "agent_followup", 2, session_id)
            adapter.tick()
            store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
            _set_last_agent(session_id, "N8N")
            return _build_extended_result({
                "model_used": "N8N",
                "response": response,
                "routed_by": "agent_followup:N8N",
                "complexity": 2,
                "cache_hit": False,
            }, memory_count=_memory_count)
        elif _last_op_agent in ("SHELL", "SELF_IMPROVE"):
            _agent_fn = run_shell_agent if _last_op_agent == "SHELL" else run_self_improve_agent
            _write_active_task(session_id, message)
            response = _safe_agent_call(_agent_fn, _followup_aug, agent_name=_last_op_agent.lower())
            _clear_active_task()
            insight_log.record(message, _last_op_agent, response, "agent_followup", 2, session_id)
            adapter.tick()
            store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
            _set_last_agent(session_id, _last_op_agent)
            return _build_extended_result({
                "model_used": _last_op_agent,
                "response": response,
                "routed_by": f"agent_followup:{_last_op_agent}",
                "complexity": 2,
                "cache_hit": False,
            }, memory_count=_memory_count)

    # ── 3b. Continuation bypass ───────────────────────────────────────────────
    # Short follow-up complaints/corrections with session history skip web_search.
    # If the session context mentions a build/APK task that is incomplete,
    # re-trigger the shell agent so it continues the work rather than just explaining.
    if _is_short_followup:
        _BUILD_CONTINUATION_HINTS = (
            "apk", "flutter", "build", "voice app", "download", "phase",
            "scaffold", "pubspec", "main.dart", "upload", "cloudinary", "github release",
        )
        _ctx_lower = (_session_ctx + " " + _active_task_desc).lower()
        _is_build_continuation = any(h in _ctx_lower for h in _BUILD_CONTINUATION_HINTS)
        # Don't hijack messages that explicitly target a different agent
        # Also don't hijack if last agent was N8N/GITHUB — context says we're not in a build
        if (
            _is_n8n_request(message) or _is_github_request(message)
            or _last_op_agent in ("N8N", "GITHUB")
        ):
            _is_build_continuation = False

        if _is_build_continuation:
            # Re-route to shell agent — call build_flutter_voice_app() unconditionally.
            # BUG 8 FIX: put user context FIRST so agent sees what was asked,
            # THEN the imperative instruction. Prevents instructions from drowning
            # out nuanced follow-ups like "just check if it's done".
            _resume_instruction = (
                f"\n\n[BUILD AGENT INSTRUCTION] The conversation above shows an in-progress "
                f"APK build. Call build_flutter_voice_app() NOW — no inspection, no questions. "
                f"build_flutter_voice_app() handles everything end-to-end. "
                f"Return only the download URL and install instructions when done."
            )
            _write_active_task(session_id, message)
            response = _safe_agent_call(run_shell_agent, _agent_message + _resume_instruction, authorized=authorized, agent_name="shell_agent")
            _clear_active_task()
            store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
            insight_log.record(message, "SHELL", response, "build_continuation", 1, session_id)
            adapter.tick()
            return _build_extended_result({
                "model_used": "SHELL",
                "response": response,
                "routed_by": "build_continuation",
                "complexity": 2,
                "cache_hit": False,
            }, memory_count=_memory_count)

        try:
            response = ask_claude(augmented_message, system=_system_claude)
        except Exception as _claude_err:
            response = f"[Claude API error: {str(_claude_err)[:200]}]"
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        insight_log.record(message, "CLAUDE", response, "continuation", 1, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "CLAUDE",
            "response": response,
            "routed_by": "continuation",
            "complexity": 1,
            "cache_hit": False,
        }, memory_count=_memory_count)

    # ── 4. Complexity score ───────────────────────────────────────────────────
    complexity = score_complexity(message)

    # ── 4a. Web search routing (Feature 1) ───────────────────────────────────
    # Guard: never misroute to web_search when a specific agent owns the request.
    # _SEARCH_KEYWORDS like "current" / "trigger" appear in n8n / shell messages.
    if _is_search_request(message) and not (
        _is_n8n_request(message)
        or _is_shell_request(message)
        or _is_github_request(message)
        or _is_self_improve_request(message)
        or _is_debug_request(message)
    ):
        from ..tools.search_tools import web_search
        results = web_search.invoke({"query": message})
        # BUG 5 FIX: include session context so follow-up search questions like
        # "what about the Python one?" have context about what was discussed before.
        _search_ctx = (
            f"[Conversation context]\n{_session_ctx[:600]}\n\n"
            if _session_ctx
            else ""
        )
        synthesis_prompt = (
            f"{_search_ctx}"
            f"Web search results for the query: '{message}'\n\n"
            f"{results}\n\n"
            f"Synthesize a clear, accurate, and concise answer. "
            f"Reference the conversation context above if relevant to this query."
        )
        response = ask_claude(synthesis_prompt, system=_system_claude)
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        insight_log.record(message, "CLAUDE+SEARCH", response, "web_search", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "CLAUDE+SEARCH",
            "response": response,
            "routed_by": "web_search",
            "complexity": complexity,
            "cache_hit": False,
        })

    # ── 4b. N8N routing (high priority — long prompts contain many keywords) ────
    # Checked before self_improve and debug so complex n8n design prompts that
    # happen to contain words like "error", "optimize", "check" are not misrouted.
    if _is_n8n_request(message):
        if _cb_is_open("n8n"):
            return _build_extended_result({
                "model_used": "N8N",
                "response": "[n8n circuit breaker open — service appears down, skipping to avoid 17s timeout. Try again in 2 minutes.]",
                "routed_by": "circuit_breaker",
                "complexity": 1,
                "cache_hit": False,
            }, routing_explanation="n8n circuit breaker is open — service appears down, request short-circuited.")
        from ..agents.n8n_agent import run_n8n_agent
        _write_active_task(session_id, message)
        response = _safe_agent_call(run_n8n_agent, _agent_message, agent_name="n8n_agent")
        _clear_active_task(session_id)
        # Record CB failure inline (can't call _agent_response_is_error — defined later in scope)
        if response and response.startswith("[") and any(
            k in response.lower() for k in ("error", "failed", "timeout", "unreachable", "refused")
        ):
            _cb_record_failure("n8n")
        insight_log.record(message, "N8N", response, "n8n_early", complexity, session_id)
        adapter.tick()
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        _vault_log_outcome("N8N", message, response, session_id)
        _set_last_agent(session_id, "N8N")
        response = _maybe_add_next_step(response, session_id, "N8N")
        _record_and_prewarm(session_id, "N8N")
        return _build_extended_result({
            "model_used": "N8N",
            "response": response,
            "routed_by": "n8n_early",
            "complexity": complexity,
            "cache_hit": False,
        }, routing_explanation=f"n8n workflow agent selected — automation keywords matched (complexity={complexity}).")

    # ── 4c. Self-improvement routing ─────────────────────────────────────────
    if _is_self_improve_request(message):
        _write_active_task(session_id, message)
        _mark_working("Self-Improve Agent", message[:100])
        response = run_self_improve_agent(_agent_message, authorized=authorized)
        _mark_done("Self-Improve Agent")
        _clear_active_task(session_id)
        insight_log.record(message, "SELF_IMPROVE", response, "self_improve", complexity, session_id)
        adapter.tick()
        _vault_log_outcome("SELF_IMPROVE", message, response, session_id)
        _set_last_agent(session_id, "SELF_IMPROVE")
        response = _maybe_add_next_step(response, session_id, "SELF_IMPROVE")
        _record_and_prewarm(session_id, "SELF_IMPROVE")
        return _build_extended_result({
            "model_used": "SELF_IMPROVE",
            "response": response,
            "routed_by": "self_improve",
            "complexity": complexity,
            "cache_hit": False,
        }, routing_explanation=f"Self-improve agent selected — infrastructure query detected (complexity={complexity}).")

    # ── 4d. Isolation debug routing ───────────────────────────────────────────
    # Guard: skip debug routing for N8N requests — "workflow isn't working" should
    # go to the n8n agent, not shell debug, even though it contains "not working".
    if _is_debug_request(message) and not _is_n8n_request(message):
        _write_active_task(session_id, message)
        response = run_shell_agent(message, authorized=authorized, debug_mode=True)
        # If shell debug itself errors, escalate straight to self-improve agent
        if response.startswith("[") and any(k in response.lower() for k in ("error", "failed", "exception")):
            response = run_self_improve_agent(
                f"The shell debug agent failed. Original debug request: {message[:200]}\n"
                f"Error: {response[:300]}\n\n"
                f"Investigate railway logs, service status, and DB health autonomously.",
                authorized=False,
            )
        _clear_active_task(session_id)
        insight_log.record(message, "SHELL", response, "isolation_debug", complexity, session_id)
        adapter.tick()
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        _vault_log_outcome("SHELL", message, response, session_id)
        _record_and_prewarm(session_id, "SHELL")
        return _build_extended_result({
            "model_used": "SHELL",
            "response": response,
            "routed_by": "isolation_debug",
            "complexity": complexity,
            "cache_hit": False,
        }, routing_explanation=f"Shell agent selected in isolation-debug mode — systematic Isolate→Identify→Fix→Integrate stance (complexity={complexity}).")

    # ── 5. Routing: AI classifier (with timeout) + keyword arbitration ───────
    #
    # Strategy (fixes priority inversion from pure-keyword approach):
    #   1. Run AI classifier with 10s timeout — gets confidence score
    #   2. Run keyword detection in parallel (instant, no network)
    #   3. Arbitration:
    #      a. AI confident (>= 0.75) → trust AI, ignore keywords
    #      b. AI uncertain (< 0.75) AND keyword matches → use keyword
    #      c. AI uncertain, no keyword → use AI route anyway (best guess)
    #   This prevents "n8n" keyword false-positives from hijacking SHELL requests
    #   that happen to mention "workflow" but are really about code/shell work.

    def _classify_with_timeout(msg: str, timeout_s: float = 5.0) -> tuple[str, float, bool]:
        """
        Run _classify_route_with_confidence with a hard timeout.
        Returns (route, confidence, timed_out). Returns GENERAL/0.0/True on timeout.
        BUG 10 FIX: returns timed_out flag so callers can tag the routing path.
        """
        import concurrent.futures as _cf2
        with _cf2.ThreadPoolExecutor(max_workers=1) as _pool2:
            fut = _pool2.submit(_classify_route_with_confidence, msg)
            try:
                route, conf = fut.result(timeout=timeout_s)
                return route, conf, False
            except _cf2.TimeoutError:
                try:
                    from ..activity_log import bg_log as _bg_cl
                    _bg_cl(f"Classifier timed out after {timeout_s}s — falling back to keyword/GENERAL", "dispatcher")
                except Exception:
                    pass
                return "GENERAL", 0.0, True
            except Exception:
                return "GENERAL", 0.0, True

    _ai_route, _ai_conf, _classifier_timed_out = _classify_with_timeout(message)
    _routing_confidence = _ai_conf
    _record_routing_confidence(_ai_route, _ai_conf)

    # Keyword detection (always instant)
    _kw_route_raw = None
    if _is_shell_request(message):
        _kw_route_raw = "SHELL"
    elif _is_github_request(message):
        _kw_route_raw = "GITHUB"
    elif _is_n8n_request(message):
        _kw_route_raw = "N8N"

    # Arbitration
    # BUG 6 FIX: high-confidence AI route is trusted UNLESS keywords strongly disagree.
    # A high-confidence wrong classification (e.g. classifier returns N8N at 0.90 but
    # keywords match GITHUB strongly) should trigger keyword override, not blind trust.
    _AGENT_ROUTES = {"SHELL", "GITHUB", "N8N", "SELF_IMPROVE"}
    _strong_kw_disagrees = (
        _kw_route_raw is not None
        and _kw_route_raw in _AGENT_ROUTES
        and _ai_route in _AGENT_ROUTES
        and _kw_route_raw != _ai_route
        and _ai_conf < 0.90  # only trust AI over keywords when it's extremely confident
    )
    if _ai_conf >= 0.75 and _ai_route not in ("GENERAL", "SEARCH") and not _strong_kw_disagrees:
        # AI is confident and keywords don't strongly disagree → trust AI
        _kw_route = _ai_route
    elif _kw_route_raw is not None:
        # AI uncertain OR keywords disagree with confident AI → use keyword
        _kw_route = _kw_route_raw
    elif _ai_conf >= 0.4 and _ai_route not in ("GENERAL", "SEARCH"):
        # AI has moderate confidence — use it
        _kw_route = _ai_route
    else:
        _kw_route = None  # falls through to complexity-based model selection

    # Low-confidence or timed-out classifier: escalate complexity
    if _ai_conf < 0.4 and _kw_route is None:
        complexity = max(complexity, 4)  # escalate to peer-review tier
    # Session context failure also escalates (set earlier in 0e)
    if _ctx_injection_failed:
        complexity = max(complexity, 3)

    # Tag the routing path for observability (Bug 10 fix)
    _classifier_tag = "classifier_timeout" if _classifier_timed_out else "classifier"

    # ── Error-interception helper ─────────────────────────────────────────────
    def _agent_response_is_error(resp: str) -> bool:
        """
        Detect when an agent returned a structural error string rather than a
        real response. These start with [ and contain error/failed/error keywords,
        OR raw API error JSON (e.g. {"type":"error",...}).
        We intercept these to trigger autonomous self-investigation before
        surfacing the error to the user.
        """
        if not resp:
            return False
        # Catch raw Anthropic API error JSON leaked from CLI
        if resp.lstrip().startswith('{"type":"error"'):
            return True
        if not resp.startswith("["):
            return False
        lower = resp.lower()
        return any(k in lower for k in (
            "error", "failed", "not set", "unreachable", "refused",
            "timeout", "not found", "unavailable", "could not", "exception",
        ))

    def _auto_investigate(failed_agent: str, original_msg: str, err_resp: str,
                          full_ctx: str = "") -> str:
        """
        When an agent fails, autonomously route to self_improve_agent with a
        diagnostic brief — it has full infrastructure access to find and fix the issue.
        Only called once (no recursion) to avoid infinite loops.
        BUG 4 FIX: accepts full_ctx (augmented_message with session history) so
        the investigation agent has the user's complete conversational context,
        not just the bare request string.
        """
        _ctx_section = (
            f"\nSession context that led to this request:\n{full_ctx[:600]}\n"
            if full_ctx and full_ctx != original_msg
            else ""
        )
        # Check vault for prior fixes to similar errors — gives the investigating agent a head start
        _vault_fix_ctx = ""
        try:
            from ..tools.obsidian_tools import obsidian_search_vault as _vs
            _vr = _vs.invoke({"query": f"error fix {failed_agent.lower()} {err_resp[:60]}"})
            if _vr and "no matches" not in _vr.lower() and len(_vr) > 20:
                _vault_fix_ctx = f"\nVault — prior fixes for similar errors:\n{_vr[:500]}\n"
        except Exception:
            pass
        brief = (
            f"AUTONOMOUS INVESTIGATION REQUIRED — {failed_agent} agent just failed.\n"
            f"User's original request: {original_msg[:300]}\n"
            f"{_ctx_section}"
            f"{_vault_fix_ctx}"
            f"Error returned: {err_resp[:400]}\n\n"
            f"Immediately investigate using your tools:\n"
            f"1. railway_get_logs + railway_get_deployment_status\n"
            f"2. db_health_check + db_get_failure_patterns\n"
            f"3. Check if the relevant service is running (n8n, code-server, uvicorn)\n"
            f"4. Distinguish retryable errors (network/timeout) from logic errors (config/code)\n"
            f"5. Apply a SAFE fix autonomously if possible\n"
            f"6. Report exactly what you found and what you did\n"
            f"Do NOT ask the user for context — you have the full error above."
        )
        try:
            investigate_result = run_self_improve_agent(brief, authorized=False)
        except Exception as _e:
            return f"{err_resp}\n\n[Auto-investigation also failed: {_e}]"

        # If investigation succeeded (not an error), retry the original agent once
        if not investigate_result.startswith("[") and failed_agent != "SELF_IMPROVE":
            try:
                _retry_fn = {
                    "SHELL": run_shell_agent,
                    "N8N": run_n8n_agent,
                    "GITHUB": run_github_agent,
                }.get(failed_agent)
                if _retry_fn:
                    _retry_result = _retry_fn(original_msg[:500], authorized=False)
                    if not _retry_result.startswith("["):
                        return (
                            f"[Auto-fixed]\n{investigate_result}\n\n---\n"
                            f"**Retry succeeded:**\n{_retry_result}"
                        )
            except Exception:
                pass
        return investigate_result

    if _kw_route == "SHELL":
        if _cb_is_open("shell"):
            return _build_extended_result({
                "model_used": "SHELL",
                "response": "[Shell circuit breaker open — service appears down, skipping to avoid timeout. Try again in 2 minutes.]",
                "routed_by": "circuit_breaker",
                "complexity": 1,
                "cache_hit": False,
            }, routing_explanation="Shell circuit breaker is open — service appears down, request short-circuited.")
        # For build requests, send the raw message only — not the augmented version.
        # Session history in augmented_message confuses the agent into inspecting
        # previous state and asking clarifying questions instead of just building.
        _BUILD_TRIGGER_WORDS = ("voice app", "android app", "apk", "build app",
                                "build the app", "flutter", "download link")
        _msg_lower = message.lower()
        _shell_payload = (
            message if any(w in _msg_lower for w in _BUILD_TRIGGER_WORDS)
            else _agent_message
        )
        _write_active_task(session_id, message)
        _mark_working("Shell Agent", message[:100])
        response = _safe_agent_call(run_shell_agent, _shell_payload, authorized=authorized, agent_name="shell_agent")
        _mark_done("Shell Agent")
        _clear_active_task(session_id)
        if _agent_response_is_error(response):
            _cb_record_failure("shell")
            _mark_error("Shell Agent", response[:200])
            response = _auto_investigate("SHELL", message, response, full_ctx=_agent_message)
        else:
            _cb_clear_failures("shell")
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        _sav = _detect_saveable_content(message, response)
        if _sav:
            store_enriched_memory(session_id, f"Q: {message[:300]} A: {response[:300]}", _sav["type"], _sav["importance"])
            if _sav["importance"] >= 4:
                response = response.rstrip() + "\n\n_Noted for future reference._"
        insight_log.record(message, "SHELL", response, "shell_keywords", complexity, session_id)
        adapter.tick()
        _vault_log_outcome("SHELL", message, response, session_id)
        _set_last_agent(session_id, "SHELL")
        response = _maybe_add_next_step(response, session_id, "SHELL")
        _record_and_prewarm(session_id, "SHELL")
        return _build_extended_result({
            "model_used": "SHELL",
            "response": response,
            "routed_by": "shell_keywords",
            "complexity": complexity,
            "cache_hit": False,
        }, routing_explanation=f"Shell/CLI agent selected (complexity={complexity}, keywords matched).")

    if _kw_route == "GITHUB":
        if _cb_is_open("github"):
            return _build_extended_result({
                "model_used": "GITHUB",
                "response": "[GitHub circuit breaker open — service appears down, skipping to avoid timeout. Try again in 2 minutes.]",
                "routed_by": "circuit_breaker",
                "complexity": 1,
                "cache_hit": False,
            }, routing_explanation="GitHub circuit breaker is open — service appears down, request short-circuited.")
        _write_active_task(session_id, message)
        _mark_working("GitHub Agent", message[:100])
        _mark_talking("Claude CLI Pro", "GitHub Agent")
        response = _safe_agent_call(run_github_agent, _agent_message, agent_name="github_agent")
        _clear_talking("Claude CLI Pro", "GitHub Agent")
        _mark_done("GitHub Agent")
        _clear_active_task(session_id)
        if _agent_response_is_error(response):
            _cb_record_failure("github")
            _mark_error("GitHub Agent", response[:200])
            response = _auto_investigate("GITHUB", message, response, full_ctx=_agent_message)
        else:
            _cb_clear_failures("github")
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        _sav = _detect_saveable_content(message, response)
        if _sav:
            store_enriched_memory(session_id, f"Q: {message[:300]} A: {response[:300]}", _sav["type"], _sav["importance"])
            if _sav["importance"] >= 4:
                response = response.rstrip() + "\n\n_Noted for future reference._"
        insight_log.record(message, "GITHUB", response, "github_keywords", complexity, session_id)
        adapter.tick()
        _vault_log_outcome("GITHUB", message, response, session_id)
        _set_last_agent(session_id, "GITHUB")
        response = _maybe_add_next_step(response, session_id, "GITHUB")
        _record_and_prewarm(session_id, "GITHUB")
        return _build_extended_result({
            "model_used": "GITHUB",
            "response": response,
            "routed_by": "github_keywords",
            "complexity": complexity,
            "cache_hit": False,
        }, routing_explanation=f"GitHub agent selected — repository operation detected (complexity={complexity}).")

    if _kw_route == "N8N":
        if _cb_is_open("n8n"):
            return _build_extended_result({
                "model_used": "N8N",
                "response": "[n8n circuit breaker open — service appears down, skipping to avoid 17s timeout. Try again in 2 minutes.]",
                "routed_by": "circuit_breaker",
                "complexity": 1,
                "cache_hit": False,
            }, routing_explanation="n8n circuit breaker is open — service appears down, request short-circuited.")
        _write_active_task(session_id, message)
        _mark_working("N8N Agent", message[:100])
        _mark_talking("Claude CLI Pro", "N8N Agent")
        response = _safe_agent_call(run_n8n_agent, _agent_message, agent_name="n8n_agent")
        _clear_talking("Claude CLI Pro", "N8N Agent")
        _mark_done("N8N Agent")
        _clear_active_task(session_id)
        if _agent_response_is_error(response):
            _cb_record_failure("n8n")
            _mark_error("N8N Agent", response[:200])
            response = _auto_investigate("N8N", message, response, full_ctx=_agent_message)
        else:
            _cb_clear_failures("n8n")
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        _sav = _detect_saveable_content(message, response)
        if _sav:
            store_enriched_memory(session_id, f"Q: {message[:300]} A: {response[:300]}", _sav["type"], _sav["importance"])
            if _sav["importance"] >= 4:
                response = response.rstrip() + "\n\n_Noted for future reference._"
        insight_log.record(message, "N8N", response, "n8n_keywords", complexity, session_id)
        adapter.tick()
        _vault_log_outcome("N8N", message, response, session_id)
        _set_last_agent(session_id, "N8N")
        response = _maybe_add_next_step(response, session_id, "N8N")
        _record_and_prewarm(session_id, "N8N")
        return _build_extended_result({
            "model_used": "N8N",
            "response": response,
            "routed_by": "n8n_keywords",
            "complexity": complexity,
            "cache_hit": False,
        }, routing_explanation=f"n8n workflow agent selected — automation keywords matched (complexity={complexity}).")

    # ── 6. Adaptive model selection ───────────────────────────────────────────
    haiku_ceiling = adapter.get_haiku_ceiling()
    suggested = model_for_complexity(complexity)
    if suggested == "HAIKU" and complexity > haiku_ceiling:
        suggested = "DEEPSEEK"

    if complexity in (3, 4):
        # Check session profile before calling Haiku classifier — saves 1 LLM call
        # per request for sessions with a stable ≥80% routing pattern.
        try:
            from ..learning.session_profile import session_profile as _sp
            _profile_hint = _sp.get_routing_hint(session_id)
        except Exception:
            _profile_hint = None

        if _profile_hint:
            model = _profile_hint
            routed_by = f"session_profile:{_profile_hint.lower()}"
        else:
            classified = classify_request(message)
            model = classified
            routed_by = _classifier_tag  # "classifier" or "classifier_timeout" (Bug 10)
    else:
        model = suggested
        routed_by = "complexity_score"

    # ── 6a. Budget guard — downgrade to cheaper models when over 80% daily budget ─
    # Only applies to non-agent conversational routes (agents need their own models).
    # CLAUDE ($4.50/M) → HAIKU ($0.40/M). DEEPSEEK stays (already cheap).
    try:
        from ..learning.cost_ledger import is_over_budget as _over_budget
        if model == "CLAUDE" and _over_budget():
            model = "HAIKU"
            routed_by = f"{routed_by}_budget_cap"
    except Exception:
        pass

    # ── 6a-2. Drift-aware model substitution ─────────────────────────────────
    # If the chosen model is currently in drift (win rate < 60% over last 100
    # exchanges), swap to the wisdom store's current best for this category.
    # This is the "learning that actually acts" fix — drift detection was already
    # logging alerts but never routing around the degraded model.
    try:
        _primary_cat_for_drift = wisdom_store._detect_category(routed_by, model)
        _drift_safe = adapter.suggest_model_avoiding_drift(_primary_cat_for_drift, model)
        if _drift_safe != model:
            from ..activity_log import bg_log as _bg_drift
            _bg_drift(
                f"Drift-avoidance: swapping {model} → {_drift_safe} "
                f"(category={_primary_cat_for_drift}, drift detected)",
                "dispatcher",
            )
            model = _drift_safe
            routed_by = f"{routed_by}_drift_swap"
    except Exception:
        pass  # Never let drift logic crash dispatch

    # ── 6b. Algorithm-store routing override ─────────────────────────────────
    # Lazy import — only loads algorithm_store after startup is complete.
    try:
        from ..learning.algorithm_store import algorithm_store as _algo_store
        _algo = _algo_store.get_algorithm("routing_heuristic")
        if _algo and _algo.ok:
            _primary_cat = wisdom_store._detect_category(routed_by, model)
            _algo_model = _algo.run(
                fn_name="recommend_model",
                category=_primary_cat,
                complexity=complexity,
            )
            if _algo_model and _algo_model.upper() in _HANDLERS and _algo_model.upper() != model:
                model = _algo_model.upper()
                routed_by = f"{routed_by}_algo"
    except Exception:
        pass  # Never let algorithm routing crash dispatch

    # ── 7. Cache lookup ───────────────────────────────────────────────────────
    if model in _CACHEABLE_MODELS:
        cached = cache.get(message, model)
        if cached:
            insight_log.record(message, model, cached, f"{routed_by}_cache", complexity, session_id)
            adapter.tick()
            return _build_extended_result({
                "model_used": model,
                "response": cached,
                "routed_by": f"{routed_by}_cache",
                "complexity": complexity,
                "cache_hit": True,
            }, routing_explanation="Cache hit — served from response cache.")

    # ── 8a. Ensemble (complexity == 5) ────────────────────────────────────────
    # BUG 9 FIX: skip ensemble when a keyword agent route is set.
    # A very complex N8N workflow request should go directly to the N8N agent
    # (which has specialized tools) rather than to multi-model voting which produces
    # text answers without tool execution.
    # _kw_route at this point should already have been consumed by the agent routing
    # above — but if complexity was set to 5 by scoring before routing, guard here too.
    if complexity == 5 and _kw_route not in ("SHELL", "N8N", "GITHUB", "SELF_IMPROVE"):
        # All 3 models talk to each other during parallel ensemble voting
        _mark_talking("Claude CLI Pro", "Gemini CLI")
        _mark_talking("Claude CLI Pro", "DeepSeek")
        _mark_talking("Gemini CLI", "DeepSeek")
        ensemble_result = ensemble_voter.vote(message, complexity, session_id)
        _clear_talking("Claude CLI Pro", "Gemini CLI")
        _clear_talking("Claude CLI Pro", "DeepSeek")
        _clear_talking("Gemini CLI", "DeepSeek")
        if ensemble_result["is_ensemble"]:
            response = ensemble_result["response"]
            if model in _CACHEABLE_MODELS:
                cache.set(message, "ENSEMBLE", response)
            insight_log.record(message, "ENSEMBLE", response, routed_by, complexity, session_id)
            adapter.tick()
            wisdom_store.record_outcome("CLAUDE", "writing/analysis", response.startswith("["))
            return _build_extended_result({
                "model_used": "ENSEMBLE",
                "response": response,
                "routed_by": routed_by,
                "complexity": complexity,
                "cache_hit": False,
            },
                is_ensemble=True,
                models_used=ensemble_result["models_used"],
                disagreement_detected=ensemble_result["disagreement_detected"],
                cloudinary_url=ensemble_result["cloudinary_url"],
                routing_explanation=f"Ensemble voting used (complexity={complexity}) — three models synthesized into one answer.",
            )

    # ── 8b. Single model call ─────────────────────────────────────────────────
    # Inject capabilities-aware system prompt for Claude/Haiku
    _worker_id = _resolve_worker(model)
    _mark_working(_worker_id, message[:100])
    if model == "CLAUDE":
        response = ask_claude(augmented_message, system=_system_claude)
    elif model == "HAIKU":
        response = ask_claude_haiku(augmented_message, system=_system_haiku)
    else:
        response = _HANDLERS[model](augmented_message)
    _mark_done(_worker_id)
    # If the model returned a structural error, surface it on the avatar immediately
    if _agent_response_is_error(response):
        _mark_error(_worker_id, response[:200])

    # Layer 1: CoT handoff (complexity >= 4, classifier-routed)
    # Visualise: reasoning model talks to answer model
    cot_result = {
        "cot_used": False, "response": "", "reasoning_model": None,
        "answer_model": None, "trace_length": 0,
    }
    if complexity >= 4 and routed_by == "classifier":
        # Map model → worker ID for talking line
        _cot_worker_map = {
            "CLAUDE": "Claude CLI Pro", "DEEPSEEK": "DeepSeek",
            "GEMINI": "Gemini CLI", "HAIKU": "Anthropic Haiku",
        }
        _cot_a = _cot_worker_map.get(model.upper(), _worker_id)
        _cot_b = "DeepSeek" if model.upper() == "CLAUDE" else "Claude CLI Pro"
        _mark_talking(_cot_a, _cot_b)
        cot_result = cot_handoff.handoff(message, model, routed_by, complexity, session_id)
        _clear_talking(_cot_a, _cot_b)
        if cot_result["cot_used"] and cot_result["response"] and not cot_result["response"].startswith("["):
            response = cot_result["response"]

    # Layer 2: Peer review (complexity >= 4)
    # Visualise: primary model talks to its critic
    review_result = {
        "was_reviewed": False, "critic_model": None,
        "critique_was_substantive": False, "critique": None,
    }
    if complexity >= 4:
        _peer_worker_map = {
            "CLAUDE": "Claude CLI Pro", "DEEPSEEK": "DeepSeek",
            "GEMINI": "Gemini CLI", "HAIKU": "Anthropic Haiku",
        }
        _critic_map = {
            "CLAUDE": "GEMINI", "DEEPSEEK": "HAIKU",
            "GEMINI": "DEEPSEEK", "HAIKU": "CLAUDE",
        }
        _peer_primary = _peer_worker_map.get(model.upper(), _worker_id)
        _peer_critic  = _peer_worker_map.get(_critic_map.get(model.upper(), ""), None)
        if _peer_critic:
            _mark_talking(_peer_primary, _peer_critic)
        review_result = peer_reviewer.review(message, response, model, complexity, session_id)
        if _peer_critic:
            _clear_talking(_peer_primary, _peer_critic)
        response = review_result["final_response"]

    # Layer 3: Red team (complexity >= 3, confidence_mode enabled)
    # Visualise: internal model challenges the answer, then escalates if flaw found
    rt_result = {"red_team_ran": False, "escalated": False, "red_verdict": None}
    if complexity >= 3:
        _mark_talking("Anthropic Haiku", _worker_id)
        rt_result = red_team.challenge(message, response, complexity, session_id)
        _clear_talking("Anthropic Haiku", _worker_id)
        if rt_result.get("escalated"):
            # Escalation: Claude CLI Pro re-answers with flaw context
            _mark_talking("Claude CLI Pro", _worker_id)
            _clear_talking("Claude CLI Pro", _worker_id)
        response = rt_result["response"]

    # ── 9. Cache + log + adapt + wisdom + memory ─────────────────────────────
    if model in _CACHEABLE_MODELS:
        cache.set(message, model, response)
    _dispatch_latency_ms = (_time.time() - _dispatch_start) * 1000
    insight_log.record(
        message, model, response, routed_by, complexity, session_id,
        latency_ms=_dispatch_latency_ms,
        confidence=_routing_confidence if _routing_confidence > 0 else None,
        memory_hits=_memory_count,
    )
    adapter.tick()
    # Feature 4: store exchange in semantic memory for future cross-session recall
    store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}",
                 source="super_agent")

    # Feature 4b: proactive memory — detect saveable content and enrich
    _saveable = _detect_saveable_content(message, response)
    if _saveable:
        store_enriched_memory(
            session_id,
            f"Q: {message[:300]} A: {response[:300]}",
            memory_type=_saveable["type"],
            importance=_saveable["importance"],
            source="super_agent",
        )
        # For high-importance items, hint to the user
        if _saveable["importance"] >= 4:
            response = response.rstrip() + "\n\n_Noted for future reference._"

    # Feature 4c: auto-insight extraction — distil exchange into reusable facts
    # Runs in daemon thread (fire-and-forget, never blocks response path).
    # Only fires for non-trivial responses to avoid burning Haiku quota on greetings.
    if complexity >= 2:
        extract_and_store_insights(message, response, model, session_id,
                                   source="auto_extract")
    # Prompt library outcome tracking (for prompt version error-rate correlation)
    if model in ("CLAUDE", "HAIKU"):
        try:
            from ..learning.prompt_library import prompt_library as _pl
            _prompt_name = "system_claude" if model == "CLAUDE" else "system_haiku"
            _pl.record_outcome(_prompt_name, response.startswith("["))
        except Exception:
            pass
    # Adaptive session routing: update per-session model profile
    try:
        from ..learning.session_profile import session_profile as _sp
        _sp.update(session_id, model, routed_by, complexity)
    except Exception:
        pass
    wisdom_store.record_outcome(
        model,
        wisdom_store._detect_category(routed_by, model),
        response.startswith("["),
    )

    # ── Vault insight hook — fire-and-forget, never blocks ───────────────
    try:
        from ..learning.vault_insight_hook import maybe_save_insight as _vault_hook
        _vault_hook(message, response, model, session_id)
    except Exception:
        pass

    # ── Routing audit trail — store routed_by + confidence in session history ──
    # Allows self_improve_agent to answer "why did you route that?" per session.
    try:
        _audit_hist = get_session_history(f"__audit_{session_id}")
        _audit_hist.add_user_message(
            f"route:{routed_by}|model:{model}|conf:{_routing_confidence:.2f}"
            f"|complexity:{complexity}|msg:{message[:80]}"
        )
    except Exception:
        pass

    # ── Proactive anomaly notice ──────────────────────────────────────────────
    if _pending_anomaly:
        response = f"⚠️ **System Alert:** {_pending_anomaly}\n\n{response}"

    return _build_extended_result({
        "model_used": model,
        "response": response,
        "routed_by": routed_by,
        "complexity": complexity,
        "cache_hit": False,
    },
        was_reviewed=review_result["was_reviewed"],
        critic_model=review_result.get("critic_model"),
        critique_was_substantive=review_result.get("critique_was_substantive", False),
        cot_used=cot_result["cot_used"],
        reasoning_model=cot_result.get("reasoning_model"),
        answer_model=cot_result.get("answer_model"),
        trace_length=cot_result.get("trace_length", 0),
        red_team_ran=rt_result["red_team_ran"],
        escalated=rt_result["escalated"],
        red_verdict=rt_result.get("red_verdict"),
        memory_count=_memory_count,
        routing_explanation=f"Routed to {model} (complexity={complexity}, classifier confidence={_routing_confidence:.0%}).",
    )
