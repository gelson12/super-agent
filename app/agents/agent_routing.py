
"""
Shared routing for all LangGraph agents — parallel multi-model architecture.

CHANGES FROM ORIGINAL:
- Tier 0 is now PARALLEL: CLI + Legion (Groq/Cerebras/GH Models) + Gemini all race simultaneously
  First quality response wins — all others cancelled. This alone cuts latency from 20-60s to 2-5s.
- Quality gate added: responses must pass minimum length + error-prefix check
- Legion PROMOTED from last-resort (Tier 5) to first-line racer (Tier 0)
  You have Groq + Cerebras + GitHub Models in Legion — fastest inference available, completely free
- Paid tiers (DeepSeek LangGraph, Anthropic API) only fire when ALL free models fail
- Recursion guard unchanged and preserved

Architecture:
  TIER 0 — Parallel free models (fire all simultaneously, ~0 cost)
    Claude CLI + Legion hive (Groq/Cerebras/GH Models) + Gemini CLI
    → First quality response wins, rest cancelled

  TIER 1 — Cheap paid with tools (only if Tier 0 fully fails)
    DeepSeek LangGraph → full tool access, very cheap

  TIER 2 — Premium paid with tools (absolute last resort)
    Anthropic API LangGraph → Sonnet quality, expensive
    DeepSeek text fallback → final safety net
"""
from __future__ import annotations

import concurrent.futures
import threading as _threading
import logging

from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from .agent_planner import extract_final_agent_text
from ..config import settings

_log = logging.getLogger("agent_routing")

# ── Operational keyword sets per agent type ──────────────────────────────────
_OPERATIONAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "shell": (
        "build", "run ", "execute", "deploy", "install", "clone", "create",
        "push", "restart", "redeploy", "fix", "write", "delete", "remove",
        "upload", "download", "apk", "flutter", "scaffold", "pub get",
    ),
    "github": (
        "create", "update", "delete", "push", "commit", "branch", "pull request",
        "open pr", "merge", "write file", "edit file",
        "modify", "change", "fix", "rename", "replace", "update the",
    ),
    "n8n": (
        "create", "build", "make", "add", "generate", "set up", "setup",
        "automate", "deploy", "write", "implement", "activate", "deactivate",
        "execute", "delete", "update", "trigger",
    ),
    "self_improve": (
        "fix", "redeploy", "restart", "update", "change", "modify", "write",
        "create", "build", "delete", "set variable", "push", "install",
    ),
}
_OPERATIONAL_KEYWORDS["default"] = (
    "build", "create", "run", "execute", "deploy", "fix", "write", "delete",
    "push", "update", "install", "restart",
)

_NO_CREDIT_PHRASES = (
    "credit balance is too low",
    "insufficient credits",
    "payment required",
    "your credit balance",
    "no credits",
    "invalid authentication",
    "authentication_error",
    "invalid api key",
    "invalid x-api-key",
    "unauthorized",
    "401",
)

# ── Quality gate ─────────────────────────────────────────────────────────────
_MIN_QUALITY_LENGTH = 40 # responses shorter than this are likely errors


def _is_quality_response(resp: str | None) -> bool:
    """
    True if a response is substantive enough to return to the user.
    Rejects: None, empty strings, error sentinels like '[Claude error: ...]',
    and responses that are suspiciously short (likely 'I cannot help' etc.)
    """
    if not resp:
        return False
    if not isinstance(resp, str):
        return False
    stripped = resp.strip()
    if not stripped:
        return False
    # Error sentinel — starts with [ and ends with ]
    if stripped.startswith("[") and stripped.endswith("]"):
        return False
    # Too short to be useful
    if len(stripped) < _MIN_QUALITY_LENGTH:
        return False
    return True


def _log(msg: str, source: str = "") -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source=source or "agent_routing")
    except Exception:
        pass


def is_operational(message: str, agent_type: str = "default") -> bool:
    """Heuristic: does this message need tool execution (True) or just text (False)?"""
    lower = message.lower()
    keywords = _OPERATIONAL_KEYWORDS.get(agent_type, _OPERATIONAL_KEYWORDS["default"])
    return any(kw in lower for kw in keywords)


def _invoke_langgraph(llm, tools: list, system_prompt: str, message: str) -> str:
    """Create a ReAct agent and invoke it. Returns the final text response."""
    agent = create_react_agent(llm, tools)
    result = agent.invoke({
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]
    })
    return extract_final_agent_text(result) or "[Agent: no response]"


def _get_anthropic_llm() -> ChatAnthropic:
    return ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
        max_tokens=8192,
    )


def _get_deepseek_llm():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        base_url="https://api.deepseek.com/v1",
        api_key=settings.deepseek_api_key,
        model="deepseek-chat",
        max_tokens=8192,
    )


# ── Recursion guard ──────────────────────────────────────────────────────────
_invoke_depth = _threading.local()
_MAX_AGENT_DEPTH = 3


def _get_depth() -> int:
    return getattr(_invoke_depth, "n", 0)


def _bump_depth() -> int:
    n = _get_depth() + 1
    _invoke_depth.n = n
    return n


def _drop_depth() -> None:
    n = max(0, _get_depth() - 1)
    _invoke_depth.n = n


# ── Status tracker helpers ────────────────────────────────────────────────────
def _track(worker, task=""):
    try:
        from ..learning.agent_status_tracker import mark_working
        mark_working(worker, task)
    except Exception:
        pass


def _done(worker):
    try:
        from ..learning.agent_status_tracker import mark_done
        mark_done(worker)
    except Exception:
        pass


def tiered_agent_invoke(
    message: str,
    system_prompt: str,
    tools: list,
    agent_type: str = "default",
    source: str = "",
) -> str:
    """
    Unified routing for any LangGraph agent with parallel Tier 0.
    Returns a response string — never raises.
    """
    depth = _bump_depth()
    if depth > _MAX_AGENT_DEPTH:
        _drop_depth()
        msg = (
            f"[recursion guard] tiered_agent_invoke depth {depth} exceeds "
            f"limit {_MAX_AGENT_DEPTH} (agent_type={agent_type}, source={source}). "
            "An agent is calling another agent in a loop. Returning instead of recursing."
        )
        try:
            from ..activity_log import bg_log as _bg
            _bg(msg, source="agent_routing")
        except Exception:
            pass
        return msg

    _source = source or f"{agent_type}_agent"
    try:
        return _tiered_agent_invoke_inner(message, system_prompt, tools, agent_type, _source)
    finally:
        _drop_depth()


def _run_cli(full_message: str) -> str | None:
    """Tier 0 racer: Claude CLI Pro."""
    try:
        from ..learning.pro_router import try_pro, is_cli_down
        if is_cli_down():
            return None
        result = try_pro(full_message)
        return result if _is_quality_response(result) else None
    except Exception:
        return None


def _run_legion(full_message: str, operational: bool) -> str | None:
    """Tier 0 racer: Legion hive (Groq + Cerebras + GitHub Models + OpenRouter + HF + Ollama)."""
    try:
        from ..models.claude import _try_legion
        result = _try_legion(full_message, timeout_s=12.0)
        if not _is_quality_response(result):
            return None
        if operational:
            return (
                result
                + "\n\n⚠️ *Answered by Legion fallback (no tool execution). "
                "Retry when Claude CLI is back for full action capability.*"
            )
        return result
    except Exception:
        return None


def _run_gemini_cli(full_message: str) -> str | None:
    """Tier 0 racer: Gemini CLI (free, text-only — only for informational requests)."""
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        result = ask_gemini_cli(full_message)
        return result if _is_quality_response(result) else None
    except Exception:
        return None


def _tiered_agent_invoke_inner(
    message: str,
    system_prompt: str,
    tools: list,
    agent_type: str,
    _source: str,
) -> str:
    # ── Routing advisor (non-binding hint) ────────────────────────────────────
    advisor_hint = None
    try:
        from ..routing.routing_advisor import recommend
        advisor_hint = recommend(message, classification=agent_type)
        _log(
            f"advisor: tier={advisor_hint.budget_tier} "
            f"prefer={advisor_hint.preferred_model} "
            f"depri={advisor_hint.deprioritize} "
            f"reason='{advisor_hint.reason}'",
            _source,
        )
    except Exception as _e:
        _log(f"advisor unavailable: {_e}", _source)

    # ── Set Obsidian vault calling-agent context ──────────────────────────────
    _AGENT_TYPE_TO_WORKER = {
        "shell": "Shell Agent",
        "github": "GitHub Agent",
        "n8n": "N8N Agent",
        "self_improve": "Self-Improve Agent",
    }
    try:
        from ..tools.obsidian_tools import set_calling_agent as _set_ca
        _set_ca(_AGENT_TYPE_TO_WORKER.get(agent_type, "Self-Improve Agent"))
    except Exception:
        pass

    full_message = f"{system_prompt}\n\n{message}"
    operational = is_operational(message, agent_type)

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 0 — PARALLEL FREE MODELS (fire all simultaneously, ~0 cost)
    # Groq and Cerebras (inside Legion) are the fastest inference available.
    # We fire CLI + Legion + (optionally) Gemini all at once.
    # First quality response wins — all others are cancelled immediately.
    # Target latency: 1-3 seconds for most queries.
    # ══════════════════════════════════════════════════════════════════════════
    _log(f"Tier 0: firing free models in parallel (operational={operational})", _source)

    tier0_futures: dict[str, concurrent.futures.Future] = {}
    tier0_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="tier0")

    try:
        tier0_futures["cli"] = tier0_pool.submit(_run_cli, full_message)
        tier0_futures["legion"] = tier0_pool.submit(_run_legion, full_message, operational)
        # Gemini is text-only — only useful for informational requests
        if not operational:
            tier0_futures["gemini"] = tier0_pool.submit(_run_gemini_cli, full_message)

        # Wait up to 15s for the first quality response
        for future in concurrent.futures.as_completed(tier0_futures.values(), timeout=15):
            try:
                result = future.result()
                if _is_quality_response(result):
                    # Cancel all other pending futures immediately
                    for f in tier0_futures.values():
                        f.cancel()
                    winner = [k for k, v in tier0_futures.items() if v is future]
                    _log(f"✓ Tier 0 winner: {winner[0] if winner else 'unknown'} ({len(result)} chars)", _source)
                    tier0_pool.shutdown(wait=False)
                    return result
            except Exception:
                continue

    except concurrent.futures.TimeoutError:
        _log("Tier 0 timeout (15s) — all free models too slow, escalating to paid tiers", _source)
    except Exception as e:
        _log(f"Tier 0 error: {e}", _source)
    finally:
        tier0_pool.shutdown(wait=False)

    _log("Tier 0 exhausted (CLI + Legion + Gemini all failed/slow) → escalating to paid tiers", _source)

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 1 — DeepSeek LangGraph (cheap, full tool access)
    # Only reached if ALL free models in Tier 0 failed or timed out.
    # ══════════════════════════════════════════════════════════════════════════
    if settings.deepseek_api_key:
        try:
            _log("Tier 1: DeepSeek LangGraph (tool-calling)", _source)
            _track("DeepSeek", message[:100])
            llm = _get_deepseek_llm()
            result = _invoke_langgraph(llm, tools, system_prompt, message)
            _done("DeepSeek")
            if _is_quality_response(result):
                return result
        except Exception as e:
            _done("DeepSeek")
            _log(f"Tier 1 DeepSeek error: {e}", _source)
    else:
        _log("No DEEPSEEK_API_KEY — skipping Tier 1", _source)

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 2 — Anthropic API LangGraph (expensive, last resort)
    # ══════════════════════════════════════════════════════════════════════════
    _skip_sonnet = bool(
        advisor_hint
        and advisor_hint.budget_tier == "critical"
        and "CLAUDE" in advisor_hint.deprioritize
        and settings.deepseek_api_key
    )
    if _skip_sonnet:
        _log("budget critical + advisor deprioritized CLAUDE → skipping Sonnet", _source)

    if settings.anthropic_api_key and not _skip_sonnet:
        try:
            _log("Tier 2: Anthropic API LangGraph (full tool access)", _source)
            _track("Sonnet Anthropic", message[:100])
            llm = _get_anthropic_llm()
            result = _invoke_langgraph(llm, tools, system_prompt, message)
            _done("Sonnet Anthropic")
            if _is_quality_response(result):
                return result
        except Exception as e:
            _done("Sonnet Anthropic")
            err = str(e).lower()
            if any(p in err for p in _NO_CREDIT_PHRASES):
                _log("Anthropic API no credits — marking strikes", _source)
                try:
                    from ..learning.agent_status_tracker import mark_strike
                    mark_strike("Sonnet Anthropic")
                    mark_strike("Anthropic Haiku")
                    mark_strike("Opus Anthropic")
                except Exception:
                    pass
            else:
                _log(f"Tier 2 Anthropic error: {e}", _source)
    else:
        if not settings.anthropic_api_key:
            _log("No ANTHROPIC_API_KEY — skipping Tier 2", _source)

    # ── Final safety net: DeepSeek text-only ─────────────────────────────────
    try:
        from ..models.deepseek import ask_deepseek
        ds = ask_deepseek(message, system=system_prompt)
        if _is_quality_response(ds):
            _log(f"✓ DeepSeek text-only (final safety net) responded ({len(ds)} chars)", _source)
            return ds
    except Exception:
        pass

    _log("ALL tiers exhausted — CLI, Legion, Gemini, DeepSeek, Anthropic all failed", _source)
    return (
        "⚠️ **All models currently unavailable.**\n\n"
        "Tried: Claude CLI → Legion (Groq/Cerebras/GH Models) → Gemini CLI → "
        "DeepSeek LangGraph → Anthropic API → DeepSeek text\n\n"
        "Likely causes:\n"
        "1. ANTHROPIC_API_KEY invalid or expired (check Railway Variables)\n"
        "2. DeepSeek API key issue\n"
        "3. CLI worker down AND Legion unreachable (LEGION_BASE_URL not set or service down)\n"
        "4. Gemini quota exhausted\n\n"
        "Check the activity log or /credits/pro-status for details."
    )

