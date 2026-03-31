from .classifier import classify_request
from .preprocessor import detect_trivial, score_complexity, model_for_complexity
from ..models.claude import ask_claude, ask_claude_haiku
from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek
from ..agents.github_agent import run_github_agent
from ..agents.shell_agent import run_shell_agent
from ..security.safe_word import check_authorization
from ..cache.response_cache import cache
from ..learning.insight_log import insight_log
from ..learning.adapter import adapter
from ..learning.wisdom_store import wisdom_store
from ..learning.peer_review import peer_reviewer
from ..learning.ensemble import ensemble_voter
from ..learning.red_team import red_team
from ..learning.cot_handoff import cot_handoff

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
}

_DEBUG_KEYWORDS = {
    "not working", "502", "503", "404", "error", "failing", "broken",
    "debug", "troubleshoot", "diagnose", "why is it", "why isn't",
    "service down", "can't connect", "connection refused", "timeout",
    "root cause", "fix the issue", "what's wrong", "why is my",
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
        response = ask_claude_haiku(message)
        cache.set(message, "HAIKU", response)
        insight_log.record(message, "HAIKU", response, "trivial", 1, session_id)
        adapter.tick()
        wisdom_store.record_outcome("HAIKU", "trivial/chat", response.startswith("["))
        return _build_extended_result({
            "model_used": "HAIKU",
            "response": response,
            "routed_by": "trivial",
            "complexity": 1,
            "cache_hit": False,
        })

    # ── 4. Complexity score ───────────────────────────────────────────────────
    complexity = score_complexity(message)

    # ── 4b. Isolation debug routing ──────────────────────────────────────────
    if _is_debug_request(message):
        response = run_shell_agent(message, authorized=authorized, debug_mode=True)
        insight_log.record(message, "SHELL", response, "isolation_debug", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "SHELL",
            "response": response,
            "routed_by": "isolation_debug",
            "complexity": complexity,
            "cache_hit": False,
        })

    # ── 5. Keyword routing ────────────────────────────────────────────────────
    if _is_shell_request(message):
        response = run_shell_agent(message, authorized=authorized)
        insight_log.record(message, "SHELL", response, "shell_keywords", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "SHELL",
            "response": response,
            "routed_by": "shell_keywords",
            "complexity": complexity,
            "cache_hit": False,
        })

    if _is_github_request(message):
        response = run_github_agent(message)
        insight_log.record(message, "GITHUB", response, "github_keywords", complexity, session_id)
        adapter.tick()
        return _build_extended_result({
            "model_used": "GITHUB",
            "response": response,
            "routed_by": "github_keywords",
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
    response = _HANDLERS[model](message)

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

    # ── 9. Cache + log + adapt + wisdom ──────────────────────────────────────
    if model in _CACHEABLE_MODELS:
        cache.set(message, model, response)
    insight_log.record(message, model, response, routed_by, complexity, session_id)
    adapter.tick()
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
    )
