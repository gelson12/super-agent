import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError
from ..config import settings
from ..prompts import SYSTEM_PROMPT_CLAUDE

_client: Anthropic | None = None

# Thread-local flag: set True when this thread fell through to Anthropic API.
# The streaming layer checks and clears this flag to emit a warning progress event.
_tls = threading.local()


def api_fallback_used() -> bool:
    """Return True if any ask_claude* call in this thread used the Anthropic API."""
    return bool(getattr(_tls, "api_used", False))


def clear_api_fallback_flag() -> None:
    """Clear the per-thread API usage flag."""
    _tls.api_used = False


def legion_used() -> bool:
    """Return True if any ask_claude* call in this thread was answered by Legion hive."""
    return bool(getattr(_tls, "legion_used", False))


def clear_legion_flag() -> None:
    _tls.legion_used = False


def _try_legion(prompt: str, timeout_s: float = 12.0) -> str | None:
    """
    Send the prompt to the Legion hive and return its winner content.

    Returns None on any failure — caller should fall through to whatever's
    next in the cascade. Never raises. HMAC-signed with
    LEGION_API_SHARED_SECRET; skipped entirely when LEGION_BASE_URL or
    LEGION_API_SHARED_SECRET are unset.
    """
    base_url = os.environ.get("LEGION_BASE_URL", "").rstrip("/")
    secret = os.environ.get("LEGION_API_SHARED_SECRET", "")
    if not base_url or not secret:
        return None
    try:
        body = json.dumps({"query": prompt, "complexity": 3}).encode()
        ts = str(int(time.time()))
        mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
        mac.update(ts.encode())
        mac.update(b"\n")
        mac.update(body)
        sig = mac.hexdigest()
        req = urllib.request.Request(
            f"{base_url}/v1/respond",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Legion-Ts": ts,
                "X-Legion-Sig": sig,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode())
        content = (data.get("content") or "").strip()
        if not content:
            return None
        _tls.legion_used = True
        return content
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    except Exception:
        return None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key, timeout=120.0)
    return _client


def ask_claude(prompt: str, system: str = SYSTEM_PROMPT_CLAUDE) -> str:
    """Send a prompt to Claude Sonnet and return the text response.

    Routing: Claude CLI (Pro/Max) → Gemini CLI (free fallback) → Anthropic API.
    Gemini is tried before the API so credits are only consumed as a last resort.
    """
    # 1. Claude CLI (Pro/Max subscription — zero API cost)
    try:
        from ..learning.pro_router import try_pro
        pro = try_pro(prompt, system=system)
        if pro is not None and not pro.lstrip().startswith('{"type":"error"'):
            return pro
    except Exception:
        pass  # pro_router unavailable — try Gemini next

    # 2. Gemini CLI (free-tier fallback — preserves Anthropic credits)
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(prompt)
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass  # Gemini unavailable — fall through to Legion next

    # 3. Legion hive (multi-agent fallback — Groq, Cerebras, GH Models,
    #    OpenRouter, HF, Ollama, Claude-B, ChatGPT — picks the best free
    #    responder before we touch Anthropic credits).
    legion = _try_legion(prompt)
    if legion is not None:
        return legion

    # 4. Last-resort Anthropic API
    if not settings.anthropic_api_key:
        return "[Claude unavailable: CLI, Gemini, and Legion all unreachable, no API key configured]"
    for _attempt in range(3):
        try:
            _tls.api_used = True
            resp = _get_client().messages.create(
                model="claude-sonnet-4-6",
                max_tokens=settings.max_tokens_claude,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except RateLimitError:
            if _attempt < 2:
                import time as _t; _t.sleep(2 ** _attempt * 3)
            else:
                return "[Claude API error: rate limit — try again shortly]"
        except Exception as e:
            return f"[Claude API fallback error: {e}]"
    return "[Claude API error: max retries exceeded]"


def ask_claude_haiku(prompt: str, system: str = SYSTEM_PROMPT_CLAUDE) -> str:
    """Send a prompt to Claude Haiku (fast, economical) and return the text response.

    Routing: Claude CLI (Pro/Max) → Gemini CLI (free fallback) → Anthropic API Haiku.
    """
    try:
        from ..learning.pro_router import try_pro
        pro = try_pro(prompt, system=system)
        if pro is not None and not pro.lstrip().startswith('{"type":"error"'):
            return pro
    except Exception:
        pass  # pro_router unavailable — try Gemini next

    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(prompt)
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass  # Gemini unavailable — fall through to Legion next

    # 3. Legion hive (multi-agent fallback — keeps n8n workflows alive during
    #    Claude CLI cooloff when Gemini is also unavailable).
    legion = _try_legion(prompt)
    if legion is not None:
        return legion

    # 4. Last-resort Anthropic API
    if not settings.anthropic_api_key:
        return "[Claude unavailable: CLI, Gemini, and Legion all unreachable, no API key configured]"
    for _attempt in range(3):
        try:
            _tls.api_used = True
            resp = _get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=settings.max_tokens_claude,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except RateLimitError:
            if _attempt < 2:
                import time as _t; _t.sleep(2 ** _attempt * 3)
            else:
                return "[Claude API error: rate limit — try again shortly]"
        except Exception as e:
            return f"[Claude API fallback error: {e}]"
    return "[Claude API error: max retries exceeded]"


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
