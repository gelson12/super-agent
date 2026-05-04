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


# Circuit breaker for Legion: open for 120s after 3 failures in a 60s window.
_legion_cb_lock = threading.Lock()
_legion_cb_failures: list[float] = []
_legion_cb_open_until: float = 0.0

def _legion_cb_record_failure() -> None:
    with _legion_cb_lock:
        now = time.monotonic()
        _legion_cb_failures[:] = [t for t in _legion_cb_failures if now - t < 60.0]
        _legion_cb_failures.append(now)
        if len(_legion_cb_failures) >= 3:
            global _legion_cb_open_until
            _legion_cb_open_until = now + 120.0

def _legion_cb_is_open() -> bool:
    return time.monotonic() < _legion_cb_open_until


def _try_legion(
    prompt: str,
    timeout_s: float | None = None,
    complexity: int = 3,
    task_kind: str = "chat",
) -> str | None:
    """
    Send the prompt to the Legion hive and return its winner content.

    Returns None on any failure — caller should fall through to whatever's
    next in the cascade. Never raises. HMAC-signed with
    LEGION_API_SHARED_SECRET; skipped entirely when LEGION_BASE_URL or
    LEGION_API_SHARED_SECRET are unset.

    timeout_s: defaults to 20s for complexity <= 3 (no deep refinement),
               35s for complexity >= 4 (CoT + critique refinement can run).
    complexity: 1-5, forwarded to Legion so it can gate refinement passes.
                Auto-elevated from the prompt when caller passes the default (3).
    """
    if _legion_cb_is_open():
        return None
    base_url = os.environ.get("LEGION_BASE_URL", "").rstrip("/")
    secret = os.environ.get("LEGION_API_SHARED_SECRET", "")
    if not base_url or not secret:
        return None

    # Auto-detect complexity from the prompt so the CoT pass fires for
    # complex queries even when the caller didn't compute it explicitly.
    # Only elevates; never lowers an explicitly-raised complexity value.
    if complexity <= 3:
        try:
            from ..routing.preprocessor import score_complexity as _sc
            complexity = max(complexity, _sc(prompt))
        except Exception:
            pass
    complexity = max(1, min(5, complexity))

    # Size the timeout to accommodate Legion's refinement layer:
    # complexity >= 4 triggers CoT + critique (up to ~30s); lower gets quick path.
    if timeout_s is None:
        if task_kind == "admin":
            timeout_s = 600.0  # infra tasks: 10-min ceiling (claude -p can take time)
        elif task_kind == "bridge_bots":
            timeout_s = 60.0   # MoA + CoT + critique at max quality
        elif complexity >= 4:
            timeout_s = 45.0
        else:
            timeout_s = 25.0
    try:
        deadline_ms = max(4000, int(timeout_s * 1000) - 1000)
        body = json.dumps({
            "query": prompt,
            "complexity": max(1, min(5, complexity)),
            "deadline_ms": deadline_ms,
            "task_kind": task_kind,
        }).encode()
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
        _legion_cb_record_failure()
        return None
    except (json.JSONDecodeError, KeyError, TypeError):
        _legion_cb_record_failure()
        return None
    except Exception:
        _legion_cb_record_failure()
        return None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key, timeout=120.0)
    return _client


def ask_claude(prompt: str, system: str = SYSTEM_PROMPT_CLAUDE) -> str:
    """Send a prompt to Claude Sonnet and return the text response.

    Routing: Claude CLI → Legion hive → Gemini CLI → Anthropic API.
    Legion is tried before Gemini because Gemini CLI has a persistent
    trust-directory block that makes it unreliable.
    """
    # 1. Claude CLI (Pro/Max subscription — zero API cost)
    _limit_phrases = ("hit your limit", "you've hit", "you have hit", "daily limit", "limit resets", "resets at", "resets in", "weekly limit")
    try:
        from ..learning.pro_router import try_pro
        pro = try_pro(prompt, system=system)
        if pro is not None and not pro.lstrip().startswith('{"type":"error"') and not pro.startswith('[') and not any(p in pro.lower() for p in _limit_phrases):
            return pro
    except Exception:
        pass  # pro_router unavailable — try Legion next

    # 2. Legion hive — multi-agent free fallback (Groq, Cerebras, GH Models,
    #    OpenRouter, HF, Ollama, Claude-B, ChatGPT). Tried before Gemini CLI
    #    because Gemini CLI has a persistent trust-directory block.
    legion = _try_legion(prompt)
    if legion is not None:
        return legion

    # 3. Gemini CLI (free-tier, kept as tertiary fallback)
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(prompt)
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass

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

    Routing: Claude CLI → Legion hive → Gemini CLI → Anthropic API Haiku.
    """
    try:
        from ..learning.pro_router import try_pro
        pro = try_pro(prompt, system=system)
        if pro is not None and not pro.lstrip().startswith('{"type":"error"') and not pro.startswith('['):
            return pro
    except Exception:
        pass

    # 2. Legion hive — free multi-agent fallback, tried before Gemini CLI
    legion = _try_legion(prompt)
    if legion is not None:
        return legion

    # 3. Gemini CLI (tertiary free fallback)
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(prompt)
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass

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
