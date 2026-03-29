from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError
from ..config import settings
from ..prompts import SYSTEM_PROMPT_CLAUDE
from ..monitoring.credits import tracker

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
        # Record token usage from response — zero extra API calls
        tracker.record(
            "CLAUDE",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
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
