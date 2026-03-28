from ..models.gemini import ask_gemini
from ..prompts import ROUTING_PROMPT

VALID_MODELS = {"GEMINI", "DEEPSEEK", "CLAUDE"}


def classify_request(request: str) -> str:
    """
    Use Gemini Flash to classify a user request into a target model.
    Returns one of: GEMINI | DEEPSEEK | CLAUDE
    Falls back to GEMINI on any unexpected output.
    """
    raw = ask_gemini(ROUTING_PROMPT.format(request=request), system="")
    result = raw.strip().upper().split()[0] if raw.strip() else ""

    # Strip trailing punctuation if present
    result = result.rstrip(".,;:")

    if result not in VALID_MODELS:
        return "GEMINI"
    return result
