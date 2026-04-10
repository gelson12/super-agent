"""
Canonical 4-tier routing fallback for all Super Agent paths.

Priority (cheapest → most expensive):
  1. Claude CLI Pro (inspiring-cat) — free, Pro Max quota
  2. Gemini CLI           — free, Google account quota
  3. Anthropic API        — paid, ANTHROPIC_API_KEY
  4. DeepSeek API         — paid, DEEPSEEK_API_KEY (last resort)

Every agent and the conversational path must use this module so the
priority order is consistent and failures are logged in one place.

Public API:
    route_text(prompt, system="", source="")  → str  (never raises)
    route_logged(prompt, system="", source="") → (str, str)  — (response, tier_used)
"""
import os


_NO_CREDIT_PHRASES = (
    "credit balance is too low",
    "insufficient credits",
    "payment required",
    "your credit balance",
    "no credits",
    "credits remaining",
)


def _log(msg: str, source: str = "") -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source=source or "routing_fallback")
    except Exception:
        pass


def route_text(prompt: str, system: str = "", source: str = "") -> str:
    """Return a response string using the first available tier."""
    result, _ = route_logged(prompt, system=system, source=source)
    return result


def route_logged(prompt: str, system: str = "", source: str = "") -> tuple[str, str]:
    """
    Return (response, tier_name) using the first available tier.
    tier_name is one of: "CLI", "GEMINI", "ANTHROPIC", "DEEPSEEK", "ERROR"
    """
    full_prompt = f"{system}\n\n{prompt}".strip() if system else prompt

    # ── Tier 1: Claude CLI Pro ────────────────────────────────────────────────
    try:
        from .pro_router import try_pro, should_attempt_cli
        if should_attempt_cli():
            result = try_pro(full_prompt)
            if result and not result.startswith("["):
                _log(f"✓ CLI Pro responded ({len(result)} chars)", source)
                return result, "CLI"
            if result:
                _log(f"CLI returned error token: {result[:120]}", source)
        else:
            _log("CLI skipped (daily/down flag active)", source)
    except Exception as e:
        _log(f"CLI exception: {e}", source)

    # ── Tier 2: Gemini CLI ────────────────────────────────────────────────────
    try:
        from .gemini_cli_worker import ask_gemini_cli
        result = ask_gemini_cli(full_prompt)
        if result and not result.startswith("["):
            _log(f"✓ Gemini CLI responded ({len(result)} chars)", source)
            return result, "GEMINI"
        if result:
            _log(f"Gemini returned error token: {result[:120]}", source)
    except Exception as e:
        _log(f"Gemini exception: {e}", source)

    # ── Tier 3: Anthropic API ─────────────────────────────────────────────────
    try:
        from ..config import settings as _s
        if _s.anthropic_api_key:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=_s.anthropic_api_key)
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8192,
                system=system or "You are Super Agent, a powerful AI assistant.",
                messages=[{"role": "user", "content": prompt}],
            )
            result = resp.content[0].text.strip()
            if result:
                _log(f"✓ Anthropic API responded ({len(result)} chars)", source)
                return result, "ANTHROPIC"
        else:
            _log("Anthropic API skipped (no API key)", source)
    except Exception as e:
        err = str(e).lower()
        if any(p in err for p in _NO_CREDIT_PHRASES):
            _log("Anthropic API has no credits — trying DeepSeek", source)
        else:
            _log(f"Anthropic API exception: {e}", source)

    # ── Tier 4: DeepSeek (last resort) ───────────────────────────────────────
    try:
        from ..config import settings as _s
        if _s.deepseek_api_key:
            from ..models.deepseek import ask_deepseek
            result = ask_deepseek(prompt, system=system or "")
            if result and not result.startswith("["):
                _log(f"✓ DeepSeek responded ({len(result)} chars)", source)
                return result, "DEEPSEEK"
            if result:
                _log(f"DeepSeek returned error token: {result[:120]}", source)
        else:
            _log("DeepSeek skipped (no DEEPSEEK_API_KEY)", source)
    except Exception as e:
        _log(f"DeepSeek exception: {e}", source)

    _log("ALL tiers failed — returning error", source)
    return "[All response tiers unavailable — CLI, Gemini, Anthropic, and DeepSeek all failed]", "ERROR"
