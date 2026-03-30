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

# Models that benefit from cache (stateless, repeatable responses)
_CACHEABLE_MODELS = {"HAIKU", "GEMINI", "DEEPSEEK", "CLAUDE"}


def _is_github_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _GITHUB_KEYWORDS)


def _is_shell_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _SHELL_KEYWORDS)


def dispatch(message: str, force_model: str | None = None, session_id: str = "default") -> dict:
    """
    Route a message to the appropriate model/agent.

    Pipeline:
    1. Safe-word guard (blocks critical write ops without authorisation)
    2. Forced model override (if caller specifies)
    3. Trivial query bypass → Haiku directly (no classifier call)
    4. Complexity scoring → suggested model tier
    5. Cache lookup (TTL 1 hour)
    6. Keyword routing: SHELL → GITHUB → classifier
    7. Adaptive model selection (respects Haiku ceiling from adapter)
    8. Response cache write + interaction logging

    Returns dict with: model_used, response, routed_by, complexity, cache_hit
    """
    # ── 1. Safe word guard ────────────────────────────────────────────────────
    authorized, block_reason = check_authorization(message)
    if not authorized:
        return {
            "model_used": "SECURITY",
            "response": block_reason,
            "routed_by": "safe_word_guard",
            "complexity": 0,
            "cache_hit": False,
        }

    # ── 2. Forced model override ──────────────────────────────────────────────
    if force_model:
        model = force_model.upper()
        if model == "SHELL":
            response = run_shell_agent(message, authorized=authorized)
            insight_log.record(message, "SHELL", response, "forced", 3, session_id)
            adapter.tick()
            return {
                "model_used": "SHELL",
                "response": response,
                "routed_by": "forced",
                "complexity": 3,
                "cache_hit": False,
            }
        if model not in _HANDLERS:
            return {
                "model_used": None,
                "response": (
                    f"Unknown model '{force_model}'. "
                    "Choose GEMINI, DEEPSEEK, CLAUDE, HAIKU, GITHUB, or SHELL."
                ),
                "routed_by": "forced",
                "complexity": 0,
                "cache_hit": False,
            }
        complexity = score_complexity(message)
        cached = cache.get(message, model) if model in _CACHEABLE_MODELS else None
        if cached:
            insight_log.record(message, model, cached, "forced_cache", complexity, session_id)
            adapter.tick()
            return {
                "model_used": model,
                "response": cached,
                "routed_by": "forced_cache",
                "complexity": complexity,
                "cache_hit": True,
            }
        response = _HANDLERS[model](message)
        if model in _CACHEABLE_MODELS:
            cache.set(message, model, response)
        insight_log.record(message, model, response, "forced", complexity, session_id)
        adapter.tick()
        return {
            "model_used": model,
            "response": response,
            "routed_by": "forced",
            "complexity": complexity,
            "cache_hit": False,
        }

    # ── 3. Trivial bypass ─────────────────────────────────────────────────────
    if detect_trivial(message):
        cached = cache.get(message, "HAIKU")
        if cached:
            insight_log.record(message, "HAIKU", cached, "trivial_cache", 1, session_id)
            adapter.tick()
            return {
                "model_used": "HAIKU",
                "response": cached,
                "routed_by": "trivial_cache",
                "complexity": 1,
                "cache_hit": True,
            }
        response = ask_claude_haiku(message)
        cache.set(message, "HAIKU", response)
        insight_log.record(message, "HAIKU", response, "trivial", 1, session_id)
        adapter.tick()
        return {
            "model_used": "HAIKU",
            "response": response,
            "routed_by": "trivial",
            "complexity": 1,
            "cache_hit": False,
        }

    # ── 4. Complexity score ───────────────────────────────────────────────────
    complexity = score_complexity(message)

    # ── 5. Keyword routing (shell → github) ───────────────────────────────────
    if _is_shell_request(message):
        response = run_shell_agent(message, authorized=authorized)
        insight_log.record(message, "SHELL", response, "shell_keywords", complexity, session_id)
        adapter.tick()
        return {
            "model_used": "SHELL",
            "response": response,
            "routed_by": "shell_keywords",
            "complexity": complexity,
            "cache_hit": False,
        }

    if _is_github_request(message):
        response = run_github_agent(message)
        insight_log.record(message, "GITHUB", response, "github_keywords", complexity, session_id)
        adapter.tick()
        return {
            "model_used": "GITHUB",
            "response": response,
            "routed_by": "github_keywords",
            "complexity": complexity,
            "cache_hit": False,
        }

    # ── 6. Adaptive model selection ───────────────────────────────────────────
    haiku_ceiling = adapter.get_haiku_ceiling()
    suggested = model_for_complexity(complexity)

    # If the adapter has lowered the ceiling, escalate from HAIKU when needed
    if suggested == "HAIKU" and complexity > haiku_ceiling:
        suggested = "DEEPSEEK"

    # Still run the LLM classifier for ambiguous mid-range queries (complexity 3–4)
    if complexity in (3, 4):
        classified = classify_request(message)
        # Classifier wins unless adapter has flagged that model as unreliable
        model = classified
        routed_by = "classifier"
    else:
        model = suggested
        routed_by = "complexity_score"

    # ── 7. Cache lookup ───────────────────────────────────────────────────────
    if model in _CACHEABLE_MODELS:
        cached = cache.get(message, model)
        if cached:
            insight_log.record(message, model, cached, f"{routed_by}_cache", complexity, session_id)
            adapter.tick()
            return {
                "model_used": model,
                "response": cached,
                "routed_by": f"{routed_by}_cache",
                "complexity": complexity,
                "cache_hit": True,
            }

    # ── 8. Call model, cache result, log ─────────────────────────────────────
    response = _HANDLERS[model](message)
    if model in _CACHEABLE_MODELS:
        cache.set(message, model, response)
    insight_log.record(message, model, response, routed_by, complexity, session_id)
    adapter.tick()

    return {
        "model_used": model,
        "response": response,
        "routed_by": routed_by,
        "complexity": complexity,
        "cache_hit": False,
    }
