from openai import OpenAI, APIError, APIConnectionError, RateLimitError
from ..config import settings
from ..prompts import SYSTEM_PROMPT_DEEPSEEK

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com",
        )
    return _client


def ask_deepseek(prompt: str, system: str = SYSTEM_PROMPT_DEEPSEEK) -> str:
    """Send a prompt to DeepSeek Chat and return the text response."""
    if not settings.deepseek_api_key:
        return "[DeepSeek error: DEEPSEEK_API_KEY not set]"
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            max_tokens=settings.max_tokens_deepseek,
            messages=messages,
        )
        return resp.choices[0].message.content.strip()
    except RateLimitError:
        return "[DeepSeek error: rate limit exceeded — try again shortly]"
    except APIConnectionError:
        return "[DeepSeek error: connection failed]"
    except APIError as e:
        return f"[DeepSeek error: {e.status_code} — {e.message}]"
