from google import genai
from google.genai import errors as genai_errors
from ..config import settings
from ..prompts import SYSTEM_PROMPT_GEMINI

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def ask_gemini(prompt: str, system: str = SYSTEM_PROMPT_GEMINI) -> str:
    """Send a prompt to Gemini Flash and return the text response."""
    if not settings.gemini_api_key:
        return "[Gemini error: GEMINI_API_KEY not set]"
    try:
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        resp = _get_client().models.generate_content(
            model="gemini-2.0-flash",
            contents=full_prompt,
        )
        return (resp.text or "").strip()
    except genai_errors.APIError as e:
        return f"[Gemini error: {e}]"
    except Exception as e:
        return f"[Gemini error: {e}]"
