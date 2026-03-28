from .classifier import classify_request
from ..models.claude import ask_claude
from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek

_HANDLERS = {
    "GEMINI": ask_gemini,
    "DEEPSEEK": ask_deepseek,
    "CLAUDE": ask_claude,
}


def dispatch(message: str, force_model: str | None = None) -> dict:
    """
    Route a message to the appropriate model.

    Args:
        message:     The user's input text.
        force_model: Optional override — one of GEMINI | DEEPSEEK | CLAUDE.
                     Skips classification when provided.

    Returns:
        dict with keys:
            model_used  — which model handled the request
            response    — the model's text output
            routed_by   — "forced" | "classifier"
    """
    if force_model:
        model = force_model.upper()
        if model not in _HANDLERS:
            return {
                "model_used": None,
                "response": f"Unknown model '{force_model}'. Choose GEMINI, DEEPSEEK, or CLAUDE.",
                "routed_by": "forced",
            }
        routed_by = "forced"
    else:
        model = classify_request(message)
        routed_by = "classifier"

    response = _HANDLERS[model](message)
    return {"model_used": model, "response": response, "routed_by": routed_by}
