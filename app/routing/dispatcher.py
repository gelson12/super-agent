from .classifier import classify_request
from ..models.claude import ask_claude
from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek
from ..agents.github_agent import run_github_agent

_HANDLERS = {
    "GEMINI": ask_gemini,
    "DEEPSEEK": ask_deepseek,
    "CLAUDE": ask_claude,
    "GITHUB": run_github_agent,
}

_GITHUB_KEYWORDS = {
    "github", "repo", "repository", "repositories", "commit", "pull request",
    "open pr", "create pr", "branch", "list files in", "read file", "create file",
    "update file", "delete file", "push to repo", "merge branch",
}


def _is_github_request(message: str) -> bool:
    lower = message.lower()
    return any(k in lower for k in _GITHUB_KEYWORDS)


def dispatch(message: str, force_model: str | None = None) -> dict:
    """
    Route a message to the appropriate model.

    Args:
        message:     The user's input text.
        force_model: Optional override — one of GEMINI | DEEPSEEK | CLAUDE | GITHUB.
                     Skips classification when provided.

    Returns:
        dict with keys:
            model_used  — which model handled the request
            response    — the model's text output
            routed_by   — "forced" | "github_keywords" | "classifier"
    """
    if force_model:
        model = force_model.upper()
        if model not in _HANDLERS:
            return {
                "model_used": None,
                "response": f"Unknown model '{force_model}'. Choose GEMINI, DEEPSEEK, CLAUDE, or GITHUB.",
                "routed_by": "forced",
            }
        routed_by = "forced"
    elif _is_github_request(message):
        model = "GITHUB"
        routed_by = "github_keywords"
    else:
        model = classify_request(message)
        routed_by = "classifier"

    response = _HANDLERS[model](message)
    return {"model_used": model, "response": response, "routed_by": routed_by}
