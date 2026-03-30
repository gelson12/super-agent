from .classifier import classify_request
from ..models.claude import ask_claude, ask_claude_haiku
from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek
from ..agents.github_agent import run_github_agent
from ..agents.shell_agent import run_shell_agent
from ..security.safe_word import check_authorization

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


def _is_github_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _GITHUB_KEYWORDS)


def _is_shell_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _SHELL_KEYWORDS)


def dispatch(message: str, force_model: str | None = None) -> dict:
    """
    Route a message to the appropriate model/agent.

    Critical write operations (GitHub writes, n8n edits, shell writes) are
    blocked unless the owner's safe word is present in the message.

    Args:
        message:     The user's input text.
        force_model: Optional override — one of GEMINI|DEEPSEEK|CLAUDE|HAIKU|GITHUB|SHELL.

    Returns:
        dict with keys: model_used, response, routed_by
    """
    # ── Safe word check (GitHub + shell write operations) ──────────────────────
    authorized, block_reason = check_authorization(message)
    if not authorized:
        return {
            "model_used": "SECURITY",
            "response": block_reason,
            "routed_by": "safe_word_guard",
        }

    # ── Forced model override ──────────────────────────────────────────────────
    if force_model:
        model = force_model.upper()
        if model == "SHELL":
            return {
                "model_used": "SHELL",
                "response": run_shell_agent(message, authorized=authorized),
                "routed_by": "forced",
            }
        if model not in _HANDLERS:
            return {
                "model_used": None,
                "response": (
                    f"Unknown model '{force_model}'. "
                    "Choose GEMINI, DEEPSEEK, CLAUDE, HAIKU, GITHUB, or SHELL."
                ),
                "routed_by": "forced",
            }
        return {
            "model_used": model,
            "response": _HANDLERS[model](message),
            "routed_by": "forced",
        }

    # ── Keyword routing (shell → github → classifier) ─────────────────────────
    if _is_shell_request(message):
        return {
            "model_used": "SHELL",
            "response": run_shell_agent(message, authorized=authorized),
            "routed_by": "shell_keywords",
        }

    if _is_github_request(message):
        return {
            "model_used": "GITHUB",
            "response": run_github_agent(message),
            "routed_by": "github_keywords",
        }

    model = classify_request(message)
    return {
        "model_used": model,
        "response": _HANDLERS[model](message),
        "routed_by": "classifier",
    }
