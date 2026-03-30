from ..models.gemini import ask_gemini
from ..prompts import ROUTING_PROMPT

VALID_MODELS = {"GEMINI", "DEEPSEEK", "CLAUDE", "HAIKU"}

# Keyword-based fallback — used when Gemini classifier is unavailable
_DEEPSEEK_KEYWORDS = {
    "code", "function", "debug", "algorithm", "math", "calculate",
    "sql", "script", "class", "json", "yaml", "regex", "error", "bug",
    "programming", "syntax", "compile", "runtime",
}
_CLAUDE_KEYWORDS = {
    "write", "draft", "summarize", "summary", "explain in depth",
    "essay", "letter", "review", "rewrite", "translate", "creative",
    "analyze", "analysis", "research", "compare", "evaluate",
}
# Everything else → Haiku (fast, cheap, handles conversational queries)


def _keyword_classify(request: str) -> str:
    lower = request.lower()
    if any(k in lower for k in _CLAUDE_KEYWORDS):
        return "CLAUDE"
    if any(k in lower for k in _DEEPSEEK_KEYWORDS):
        return "DEEPSEEK"
    return "HAIKU"


def classify_request(request: str) -> str:
    """
    Use Gemini Flash to classify a user request into a target model.
    Falls back to keyword classification if Gemini is unavailable.
    Returns one of: GEMINI | DEEPSEEK | CLAUDE | HAIKU
    """
    raw = ask_gemini(ROUTING_PROMPT.format(request=request), system="")

    if raw.startswith("[Gemini error"):
        return _keyword_classify(request)

    result = raw.strip().upper().split()[0] if raw.strip() else ""
    result = result.rstrip(".,;:")

    if result not in VALID_MODELS:
        return _keyword_classify(request)
    return result
