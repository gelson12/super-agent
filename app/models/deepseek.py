from openai import OpenAI, APIError, APIConnectionError, RateLimitError
from ..config import settings
from ..prompts import SYSTEM_PROMPT_DEEPSEEK

_client: OpenAI | None = None

_NO_BALANCE_PHRASES = ("insufficient balance", "insufficient_balance", "account balance", "balance is not enough")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com",
        )
    return _client


def _mark(state: str) -> None:
    try:
        from ..learning.agent_status_tracker import mark_strike, mark_done
        if state == "strike":
            mark_strike("DeepSeek")
        else:
            mark_done("DeepSeek")
    except Exception:
        pass


def check_deepseek_balance() -> dict:
    """
    Query DeepSeek balance endpoint. Returns dict with keys:
      available (bool), balance_usd (float|None), error (str|None)
    Also calls mark_strike/mark_done so the dashboard widget reflects credit state.
    """
    if not settings.deepseek_api_key:
        return {"available": False, "balance_usd": None, "error": "DEEPSEEK_API_KEY not set"}
    try:
        import httpx
        r = httpx.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            # Response: {"is_available": bool, "balance_infos": [{"currency": "USD", "total_balance": "...", ...}]}
            available = data.get("is_available", False)
            balance = None
            for b in data.get("balance_infos", []):
                if b.get("currency") == "USD":
                    try:
                        balance = float(b.get("total_balance", 0))
                    except (ValueError, TypeError):
                        pass
                    break
            if available:
                _mark("done")
            else:
                _mark("strike")
            return {"available": available, "balance_usd": balance, "error": None}
        else:
            return {"available": None, "balance_usd": None, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"available": None, "balance_usd": None, "error": str(e)}


def ask_deepseek(prompt: str, system: str = "") -> str:
    """Send a prompt to DeepSeek Chat and return the text response."""
    if not system:
        system = SYSTEM_PROMPT_DEEPSEEK
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
        _mark("done")
        return resp.choices[0].message.content.strip()
    except RateLimitError as e:
        msg = str(e).lower()
        if any(p in msg for p in _NO_BALANCE_PHRASES):
            _mark("strike")
            return "[DeepSeek error: insufficient balance — top up at platform.deepseek.com]"
        return "[DeepSeek error: rate limit exceeded — try again shortly]"
    except APIConnectionError:
        return "[DeepSeek error: connection failed]"
    except APIError as e:
        msg = (str(e.message) if hasattr(e, "message") else str(e)).lower()
        if e.status_code == 402 or any(p in msg for p in _NO_BALANCE_PHRASES):
            _mark("strike")
            return "[DeepSeek error: insufficient balance — top up at platform.deepseek.com]"
        return f"[DeepSeek error: {e.status_code} — {e.message}]"
