import base64
from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError
from ..config import settings
from ..prompts import SYSTEM_PROMPT_CLAUDE

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


def ask_claude(prompt: str, system: str = SYSTEM_PROMPT_CLAUDE) -> str:
    """Send a prompt to Claude Sonnet and return the text response."""
    if not settings.anthropic_api_key:
        return "[Claude error: ANTHROPIC_API_KEY not set]"
    try:
        resp = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=settings.max_tokens_claude,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()
    except RateLimitError:
        return "[Claude error: rate limit exceeded — try again shortly]"
    except APIConnectionError:
        return "[Claude error: connection failed]"
    except APIError as e:
        return f"[Claude error: {e.status_code} — {e.message}]"


def ask_claude_haiku(prompt: str, system: str = SYSTEM_PROMPT_CLAUDE) -> str:
    """Send a prompt to Claude Haiku (fast, economical) and return the text response."""
    if not settings.anthropic_api_key:
        return "[Claude error: ANTHROPIC_API_KEY not set]"
    try:
        resp = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=settings.max_tokens_claude,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()
    except RateLimitError:
        return "[Claude error: rate limit exceeded — try again shortly]"
    except APIConnectionError:
        return "[Claude error: connection failed]"
    except APIError as e:
        return f"[Claude error: {e.status_code} — {e.message}]"


def ask_claude_vision(image_bytes: bytes, media_type: str, text: str = "") -> str:
    """Send an image to Claude Vision and return the text response."""
    if not settings.anthropic_api_key:
        return "[Claude error: ANTHROPIC_API_KEY not set]"
    try:
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                },
            },
            {"type": "text", "text": text if text else "Describe this image in detail."},
        ]
        resp = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=settings.max_tokens_claude,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()
    except RateLimitError:
        return "[Claude error: rate limit exceeded — try again shortly]"
    except APIConnectionError:
        return "[Claude error: connection failed]"
    except APIError as e:
        return f"[Claude error: {e.status_code} — {e.message}]"
