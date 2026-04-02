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
from ..memory.vector_memory import get_memory_context, store_memory
from ..memory.session import get_compressed_context
from ..prompts import build_capabilities_block, SYSTEM_PROMPT_CLAUDE, SYSTEM_PROMPT_HAIKU
from ..config import settings as _settings

_HANDLERS = {
    "GEMINI":   ask_gemini,
    "DEEPSEEK": ask_deepseek,
    "CLAUDE":   ask_claude,
    "HAIKU":    ask_claude_haiku,
    "GITHUB":   run_github_agent,
}

_GITHUB_KEYWORDS = {
    "github", "repo", "repository", "repositories", "commit", "pull request",
    "open pr", "create pr", "branch", "list files in", "read file", "create file",
    "update file", "delete file", "push to repo", "merge branch",
}

_SHELL_KEYWORDS = {
    "terminal", "shell", "run command", "execute", "workspace",
    "clone repo", "clone the repo", "list workspace", "ls /", "git clone",
    "fix the code", "auto fix", "autofix", "run the tests", "install package",
    "claude cli", "run claude",
    # Flutter / mobile build
    "flutter", "apk", "build apk", "build android", "build the app",
    "build app", "android app", "mobile app", "dart",
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
    "n8n", "workflow", "workflows", "automation", "trigger",
    "webhook", "execution", "executions",
    "create workflow", "update workflow", "activate workflow",
    "deactivate workflow", "run workflow", "debug workflow",
    "list workflows", "get workflow",
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


def _is_self_improve_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _SELF_IMPROVE_KEYWORDS)


def _is_search_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _SEARCH_KEYWORDS)


def _classify_route_with_confidence(message: str) -> tuple[str, float]:
    """
    Feature 5: Use Haiku to classify the route with a confidence score.
    Falls back to ("GENERAL", 0.0) on any error — never blocks dispatch.
    ~200ms overhead — only called when no keyword match was found.
    """
    prompt = (
        f'Classify this user message into exactly one category.\n'
        f'Message: "{message}"\n\n'
        f'Categories: SHELL, GITHUB, N8N, SELF_IMPROVE, SEARCH, GENERAL\n'
        f'Confidence: 0.0 to 1.0\n\n'
        f'Reply in exactly this format:\n'
        f'CATEGORY: <category>\n'
        f'CONFIDENCE: <0.0-1.0>'
    )
    try:
        result = ask_claude_haiku(prompt)
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
    except Exception:
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
        **kwargs,
    }


# ── Main dispatch function ────────────────────────────────────────────────────

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

    # ── 0. Proactive cross-session memory injection ───────────────────────────
    # Always retrieve relevant past memories regardless of whether pgvector is up.
    # The JSON fallback in vector_memory.py ensures this always returns something
    # after the first few exchanges.
    memory_ctx = get_memory_context(message)
    _memory_count = memory_ctx.count("\n-") if memory_ctx else 0
    augmented_message = (memory_ctx + message) if memory_ctx else message

    # ── 0b. Session history injection ─────────────────────────────────────────
    # Prepend compressed conversation history so models have full in-session context
    _session_ctx = ""
    try:
        _session_ctx = get_compressed_context(session_id)
        if _session_ctx:
            augmented_message = (
                f"[Conversation history — this session]\n{_session_ctx}\n\n"
                f"[Current message]\n{augmented_message}"
            )
    except Exception:
        pass  # Never let session history crash dispatch

    # ── 0c-extra. Continuation detection ─────────────────────────────────────
    # Short follow-up messages ("nothing", "you didn't give", "what about",
    # "fix that", "try again") have no keywords and get mis-routed to web_search.
    # If the session has recent history AND the message is short + complaint-like,
    # inject full session context and route to Claude directly rather than searching.
    _CONTINUATION_PATTERNS = (
        "didn't", "did not", "nothing", "no link", "no download", "failed",
        "wrong", "incorrect", "try again", "retry", "what about", "and the",
        "you forgot", "you missed", "you didn't", "fix that", "what happened",
        "still", "but you", "you said", "as i said", "i said",
    )
    _is_short_followup = (
        len(message.split()) <= 20
        and _session_ctx
        and any(p in message.lower() for p in _CONTINUATION_PATTERNS)
    )

    # ── 0c. Build capabilities-aware system prompts ───────────────────────────
    _caps = build_capabilities_block(_settings)
    _learned = adapter.get_learned_context() or ""
    _system_claude = SYSTEM_PROMPT_CLAUDE.format(
        capabilities=_caps,
        learned_context=_learned,
    )
    _system_haiku = SYSTEM_PROMPT_HAIKU.format(
        capabilities=_caps,
        learned_context=_learned,
    )

    # ── 1. Safe word guard ────────────────────────────────────────────────────
    authorized, block_reason = check_authorization(message)
    if not authorized:
        return _build_extended_result({
            "model_used": "SECURITY",
            "response": block_reason,
            "routed_by": "safe_word_guard",
            "complexity": 0,
            "cache_hit": False,
        })

    # ── 2. Forced model override ──────────────────────────────────────────────
    if force_model:
        model = force_model.upper()
        if model == "SHELL":
            response = run_shell_agent(message, authorized=authorized)
            insight_log.record(message, "SHELL", response, "forced", 3, session_id)
            adapter.tick()
            return _build_extended_result({
                "model_used": "SHELL",
                "response": response,
                "routed_by": "forced",
                "complexity": 3,
                "cache_hit": False,
            })
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
            })
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
            })
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
        })

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
            })
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

    # ── 3b. Continuation bypass ───────────────────────────────────────────────
    # Short follow-up complaints/corrections with session history are handed directly
    # to Claude with full context — prevents them from hitting web_search or trivial.
    if _is_short_followup:
        response = ask_claude(augmented_message, system=_system_claude)
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
    if _is_search_request(message):
        from ..tools.search_tools import web_search
        results = web_search.invoke({"query": message})
        synthesis_prompt = (
            f"Web search results for the query: '{message}'\n\n"
            f"{results}\n\n"
            f"Synthesize a clear, accurate, and concise answer based on these results."
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

    # ── 4b. Self-improvement routing (highest priority after debug) ──────────
    if _is_self_improve_request(message):
        response = run_self_improve_agent(message, authorized=authorized)
        insight_log.record(message, "SELF_IMPROVE", response, "self_improve", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "SELF_IMPROVE",
            "response": response,
            "routed_by": "self_improve",
            "complexity": complexity,
            "cache_hit": False,
        })

    # ── 4c. Isolation debug routing ───────────────────────────────────────────
    if _is_debug_request(message):
        response = run_shell_agent(message, authorized=authorized, debug_mode=True)
        # If shell debug itself errors, escalate straight to self-improve agent
        if response.startswith("[") and any(k in response.lower() for k in ("error", "failed", "exception")):
            response = run_self_improve_agent(
                f"The shell debug agent failed. Original debug request: {message[:200]}\n"
                f"Error: {response[:300]}\n\n"
                f"Investigate railway logs, service status, and DB health autonomously.",
                authorized=False,
            )
        insight_log.record(message, "SHELL", response, "isolation_debug", complexity, session_id)
        adapter.tick()
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        return _build_extended_result({
            "model_used": "SHELL",
            "response": response,
            "routed_by": "isolation_debug",
            "complexity": complexity,
            "cache_hit": False,
        })

    # ── 5. Keyword routing + confidence boost (Features 1 & 5) ───────────────
    # Keywords give immediate 1.0 confidence. If no keyword matches, Haiku
    # classifies with a confidence score — only routes if >= 0.7.
    _kw_route = None
    if _is_shell_request(message):
        _kw_route = "SHELL"
    elif _is_github_request(message):
        _kw_route = "GITHUB"
    elif _is_n8n_request(message):
        _kw_route = "N8N"

    if _kw_route is None:
        # No keyword match — ask Haiku to classify (Feature 5)
        _ai_route, _ai_conf = _classify_route_with_confidence(message)
        if _ai_conf >= 0.7 and _ai_route not in ("GENERAL", "SEARCH"):
            _kw_route = _ai_route

    # ── Error-interception helper ─────────────────────────────────────────────
    def _agent_response_is_error(resp: str) -> bool:
        """
        Detect when an agent returned a structural error string rather than a
        real response. These start with [ and contain error/failed/error keywords.
        We intercept these to trigger autonomous self-investigation before
        surfacing the error to the user.
        """
        if not resp or not resp.startswith("["):
            return False
        lower = resp.lower()
        return any(k in lower for k in (
            "error", "failed", "not set", "unreachable", "refused",
            "timeout", "not found", "unavailable", "could not", "exception",
        ))

    def _auto_investigate(failed_agent: str, original_msg: str, err_resp: str) -> str:
        """
        When an agent fails, autonomously route to self_improve_agent with a
        diagnostic brief — it has full infrastructure access to find and fix the issue.
        Only called once (no recursion) to avoid infinite loops.
        """
        brief = (
            f"AUTONOMOUS INVESTIGATION REQUIRED — {failed_agent} agent just failed.\n"
            f"User's original request: {original_msg[:200]}\n"
            f"Error returned: {err_resp[:300]}\n\n"
            f"Immediately investigate using your tools:\n"
            f"1. railway_get_logs + railway_get_deployment_status\n"
            f"2. db_health_check + db_get_failure_patterns\n"
            f"3. Check if the relevant service is running (n8n, code-server, uvicorn)\n"
            f"4. Apply a SAFE fix autonomously if possible\n"
            f"5. Report exactly what you found and what you did\n"
            f"Do NOT ask the user for context — you have the full error above."
        )
        try:
            return run_self_improve_agent(brief, authorized=False)
        except Exception as _e:
            return f"{err_resp}\n\n[Auto-investigation also failed: {_e}]"

    if _kw_route == "SHELL":
        response = run_shell_agent(augmented_message, authorized=authorized)
        if _agent_response_is_error(response):
            response = _auto_investigate("SHELL", message, response)
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        insight_log.record(message, "SHELL", response, "shell_keywords", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "SHELL",
            "response": response,
            "routed_by": "shell_keywords",
            "complexity": complexity,
            "cache_hit": False,
        })

    if _kw_route == "GITHUB":
        response = run_github_agent(augmented_message)
        if _agent_response_is_error(response):
            response = _auto_investigate("GITHUB", message, response)
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        insight_log.record(message, "GITHUB", response, "github_keywords", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "GITHUB",
            "response": response,
            "routed_by": "github_keywords",
            "complexity": complexity,
            "cache_hit": False,
        })

    if _kw_route == "N8N":
        response = run_n8n_agent(augmented_message)
        if _agent_response_is_error(response):
            response = _auto_investigate("N8N", message, response)
        store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
        insight_log.record(message, "N8N", response, "n8n_keywords", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "N8N",
            "response": response,
            "routed_by": "n8n_keywords",
            "complexity": complexity,
            "cache_hit": False,
        })

    # ── 6. Adaptive model selection ───────────────────────────────────────────
    haiku_ceiling = adapter.get_haiku_ceiling()
    suggested = model_for_complexity(complexity)
    if suggested == "HAIKU" and complexity > haiku_ceiling:
        suggested = "DEEPSEEK"

    if complexity in (3, 4):
        classified = classify_request(message)
        model = classified
        routed_by = "classifier"
    else:
        model = suggested
        routed_by = "complexity_score"

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
            })

    # ── 8a. Ensemble (complexity == 5) ────────────────────────────────────────
    if complexity == 5:
        ensemble_result = ensemble_voter.vote(message, complexity, session_id)
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
            )

    # ── 8b. Single model call ─────────────────────────────────────────────────
    # Inject capabilities-aware system prompt for Claude/Haiku
    if model == "CLAUDE":
        response = ask_claude(augmented_message, system=_system_claude)
    elif model == "HAIKU":
        response = ask_claude_haiku(augmented_message, system=_system_haiku)
    else:
        response = _HANDLERS[model](augmented_message)

    # Layer 1: CoT handoff (complexity >= 4, classifier-routed)
    cot_result = {
        "cot_used": False, "response": "", "reasoning_model": None,
        "answer_model": None, "trace_length": 0,
    }
    if complexity >= 4 and routed_by == "classifier":
        cot_result = cot_handoff.handoff(message, model, routed_by, complexity, session_id)
        if cot_result["cot_used"] and cot_result["response"] and not cot_result["response"].startswith("["):
            response = cot_result["response"]

    # Layer 2: Peer review (complexity >= 4)
    review_result = {
        "was_reviewed": False, "critic_model": None,
        "critique_was_substantive": False, "critique": None,
    }
    if complexity >= 4:
        review_result = peer_reviewer.review(message, response, model, complexity, session_id)
        response = review_result["final_response"]

    # Layer 3: Red team (complexity >= 3, confidence_mode enabled)
    rt_result = {"red_team_ran": False, "escalated": False, "red_verdict": None}
    if complexity >= 3:
        rt_result = red_team.challenge(message, response, complexity, session_id)
        response = rt_result["response"]

    # ── 9. Cache + log + adapt + wisdom + memory ─────────────────────────────
    if model in _CACHEABLE_MODELS:
        cache.set(message, model, response)
    insight_log.record(message, model, response, routed_by, complexity, session_id)
    adapter.tick()
    # Feature 4: store exchange in semantic memory for future cross-session recall
    store_memory(session_id, f"Q: {message[:300]} A: {response[:300]}")
    wisdom_store.record_outcome(
        model,
        wisdom_store._detect_category(routed_by, model),
        response.startswith("["),
    )

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
    )
