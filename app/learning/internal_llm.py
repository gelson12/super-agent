"""
Internal LLM cascade — CLI-first for all background/internal tasks.

Use ask_internal() for ANY internal operation:
  nightly review, weekly review, benchmarks, peer review, red team,
  ensemble synthesis, improvement voting, session summarisation, etc.

Cascade order (mirrors the user-facing classifier):
  1. Claude CLI Pro  (ask_claude_code)  — OAuth subscription, zero extra cost
  2. Gemini CLI      (ask_gemini_cli)   — free ~1500 req/day
  3. Haiku API       (ask_claude_haiku) — last resort, costs tokens

Never raises — returns an error string prefixed with "[" on total failure.

NOTE: CLI workers do not accept a separate system prompt.
If a system prompt is provided it is prepended to the user prompt so the
model still sees the context, just in a single string.
"""

from __future__ import annotations


def _merge(prompt: str, system: str) -> str:
    """Merge system + user prompt into a single string for CLI workers."""
    if not system or not system.strip():
        return prompt
    return f"{system.strip()}\n\n---\n\n{prompt}"


def ask_internal(prompt: str, system: str = "") -> str:
    """
    CLI-first LLM call for internal/background operations.

    Tries Claude CLI Pro → Gemini CLI → Haiku API in order.
    Returns the first successful response.
    """
    full_prompt = _merge(prompt, system)

    # ── Tier 1: Claude CLI Pro ────────────────────────────────────────────────
    try:
        from .claude_code_worker import ask_claude_code
        result = ask_claude_code(full_prompt)
        if result and not result.startswith("["):
            return result
    except Exception:
        pass

    # ── Tier 2: Gemini CLI ────────────────────────────────────────────────────
    try:
        from .gemini_cli_worker import ask_gemini_cli
        result = ask_gemini_cli(full_prompt)
        if result and not result.startswith("["):
            return result
    except Exception:
        pass

    # ── Tier 3: Haiku API (last resort) ──────────────────────────────────────
    try:
        from ..models.claude import ask_claude_haiku
        return ask_claude_haiku(prompt, system=system) if system else ask_claude_haiku(prompt)
    except Exception as e:
        return f"[ask_internal: all tiers failed — {e}]"


def ask_internal_fast(prompt: str, system: str = "") -> str:
    """
    Same cascade but skips Gemini (faster, for latency-sensitive internal calls
    like session summarisation and adjudication). Falls back Tier 1 → Tier 3.
    """
    full_prompt = _merge(prompt, system)

    try:
        from .claude_code_worker import ask_claude_code
        result = ask_claude_code(full_prompt)
        if result and not result.startswith("["):
            return result
    except Exception:
        pass

    try:
        from ..models.claude import ask_claude_haiku
        return ask_claude_haiku(prompt, system=system) if system else ask_claude_haiku(prompt)
    except Exception as e:
        return f"[ask_internal_fast: all tiers failed — {e}]"
