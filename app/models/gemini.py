from google import genai
from google.genai import errors as genai_errors, types as genai_types
from ..config import settings
from ..prompts import SYSTEM_PROMPT_GEMINI

_client: genai.Client | None = None

# Ordered by preference — newest first, falls back automatically if unavailable
GEMINI_MODEL_PRIORITY = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]

_working_model: str | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _detect_working_model() -> str | None:
    """
    Try each model in GEMINI_MODEL_PRIORITY with a minimal test prompt.
    Cache the first one that works and reuse it for all subsequent calls.
    Returns None if no model is available.
    """
    global _working_model
    if _working_model:
        return _working_model

    client = _get_client()
    for model in GEMINI_MODEL_PRIORITY:
        try:
            resp = client.models.generate_content(
                model=model,
                contents="hi",
            )
            if getattr(resp, "text", None) is not None:
                _working_model = model
                return model
        except genai_errors.APIError as e:
            # 404 = model not available, 429 = quota, try next
            if hasattr(e, "code") and e.code not in (404, 429):
                break
            continue
        except Exception:
            continue
    return None


def ask_gemini(prompt: str, system: str = SYSTEM_PROMPT_GEMINI) -> str:
    """
    Send a prompt to the best available Gemini model.
    Automatically detects and caches which model works on this API key.
    """
    if not settings.gemini_api_key:
        return "[Gemini error: GEMINI_API_KEY not set]"

    model = _detect_working_model()
    if model is None:
        return "[Gemini error: no available model found — check your API key and billing]"

    try:
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        resp = _get_client().models.generate_content(
            model=model,
            contents=full_prompt,
        )
        return (resp.text or "").strip()
    except genai_errors.APIError as e:
        # If the cached model stops working, reset and let it re-detect next call
        global _working_model
        _working_model = None
        return f"[Gemini error: {e}]"
    except Exception as e:
        return f"[Gemini error: {e}]"


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    """Transcribe audio using Gemini's native audio understanding."""
    if not settings.gemini_api_key:
        return "[Gemini error: GEMINI_API_KEY not set]"

    model = _detect_working_model()
    if model is None:
        return "[Gemini error: no available model found]"

    try:
        audio_part = genai_types.Part(
            inline_data=genai_types.Blob(mime_type=mime_type, data=audio_bytes)
        )
        text_part = genai_types.Part(
            text="Transcribe this audio accurately. Return only the spoken words, nothing else."
        )
        resp = _get_client().models.generate_content(
            model=model,
            contents=genai_types.Content(role="user", parts=[audio_part, text_part]),
        )
        return (resp.text or "").strip()
    except Exception as e:
        return f"[Gemini transcription error: {e}]"
