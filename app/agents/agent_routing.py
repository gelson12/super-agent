"""
Shared 4-tier routing for all LangGraph agents.

Solves: all agents duplicate the same CLI → Gemini → API → DeepSeek fallback
chain, and text-only tiers (CLI/Gemini) waste calls on operational requests
that need tool execution.

Public API:
    tiered_agent_invoke(message, system_prompt, tools, agent_type, source="")
        → str  (never raises)

Routing logic:
  - INFORMATIONAL requests (read, describe, explain, list, status, check):
      Tier 1: CLI Pro → Tier 2: Gemini CLI → Tier 3: LangGraph Anthropic API
      → Tier 3b: LangGraph DeepSeek → error

  - OPERATIONAL requests (build, create, push, deploy, fix, run, delete, update):
      Skip text-only tiers → Tier 3: LangGraph Anthropic API
      → Tier 3b: LangGraph DeepSeek → error
"""
from __future__ import annotations

from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from .agent_planner import extract_final_agent_text
from ..config import settings


# ── Operational keyword sets per agent type ──────────────────────────────────
# If the message contains any of these, skip text-only tiers (CLI/Gemini)
# because the request requires tool execution.

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

# Fallback for unknown agent types — conservative, skips text tiers for anything
# that looks like a write/action request.
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


def _invoke_langgraph(
    llm,
    tools: list,
    system_prompt: str,
    message: str,
) -> str:
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
    """Return a LangGraph-compatible ChatModel backed by DeepSeek's OpenAI-compatible API."""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        base_url="https://api.deepseek.com/v1",
        api_key=settings.deepseek_api_key,
        model="deepseek-chat",
        max_tokens=8192,
    )


def tiered_agent_invoke(
    message: str,
    system_prompt: str,
    tools: list,
    agent_type: str = "default",
    source: str = "",
) -> str:
    """
    Unified 4-tier routing for any LangGraph agent.

    For informational requests:
      Tier 1 (CLI) → Tier 2 (Gemini) → Tier 3 (Anthropic LangGraph) → Tier 3b (DeepSeek LangGraph)

    For operational requests (need tools):
      Tier 3 (Anthropic LangGraph) → Tier 3b (DeepSeek LangGraph)

    Returns a response string — never raises.
    """
    _source = source or f"{agent_type}_agent"

    # ── Status tracker helpers (for Anthropic/DeepSeek LangGraph tiers) ──
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

    # ── Tier 1: Claude CLI Pro (ALWAYS try first — it's free) ────────────
    # CLI Pro on inspiring-cat has shell access, n8n MCP, and full tool
    # capability. It's FREE so we should always prefer it over paid API.
    # Only skip if the CLI is genuinely down (not just rate-limited).
    try:
        from ..learning.pro_router import try_pro, should_attempt_cli, is_cli_down
        # Always attempt CLI unless genuinely unreachable — burst/daily
        # cooldowns should NOT push traffic to the paid Anthropic API
        if not is_cli_down():
            cli_result = try_pro(f"{system_prompt}\n\n{message}")
            if cli_result and not cli_result.startswith("["):
                _log(f"✓ CLI Pro responded ({len(cli_result)} chars)", _source)
                return cli_result
            _log(f"CLI returned error/empty — trying Gemini", _source)
        else:
            _log("CLI genuinely down — skipping to Gemini", _source)
    except Exception as e:
        _log(f"CLI exception: {e} — trying Gemini", _source)

    # ── Tier 2: Gemini CLI (free, text-only — try for informational) ────
    operational = is_operational(message, agent_type)
    if not operational:
        try:
            from ..learning.gemini_cli_worker import ask_gemini_cli
            gemini = ask_gemini_cli(f"{system_prompt}\n\n{message}")
            if gemini and not gemini.startswith("["):
                _log(f"✓ Gemini CLI responded ({len(gemini)} chars)", _source)
                return gemini
            _log(f"Gemini returned error/empty — trying DeepSeek", _source)
        except Exception as e:
            _log(f"Gemini exception: {e} — trying DeepSeek", _source)
    else:
        _log(f"Operational request — skipping Gemini (no tools), trying DeepSeek LangGraph", _source)

    # ── Tier 3: LangGraph + DeepSeek (cheap, full tool access) ──────────
    # DeepSeek is cheaper than Anthropic — try it first
    if settings.deepseek_api_key:
        try:
            _log(f"Using LangGraph (DeepSeek) — tool-calling fallback", _source)
            _track("DeepSeek", message[:100])
            llm = _get_deepseek_llm()
            result = _invoke_langgraph(llm, tools, system_prompt, message)
            _done("DeepSeek")
            return result
        except Exception as e:
            _done("DeepSeek")
            _log(f"DeepSeek LangGraph error: {e} — trying Anthropic API", _source)
    else:
        _log("No DEEPSEEK_API_KEY — skipping to Anthropic API", _source)

    # ── Tier 4: LangGraph + Anthropic API (expensive, last resort) ──────
    if settings.anthropic_api_key:
        try:
            _log(f"Using LangGraph (Anthropic API) — full tool access", _source)
            _track("Sonnet Anthropic", message[:100])
            llm = _get_anthropic_llm()
            result = _invoke_langgraph(llm, tools, system_prompt, message)
            _done("Sonnet Anthropic")
            return result
        except Exception as e:
            _done("Sonnet Anthropic")
            err = str(e).lower()
            if any(p in err for p in _NO_CREDIT_PHRASES):
                _log("Anthropic API no credits — trying DeepSeek LangGraph", _source)
                try:
                    from ..learning.agent_status_tracker import mark_strike
                    mark_strike("Sonnet Anthropic")
                    mark_strike("Anthropic Haiku")
                    mark_strike("Opus Anthropic")
                except Exception:
                    pass
            else:
                _log(f"Anthropic API error: {e}", _source)
    else:
        _log("No ANTHROPIC_API_KEY — skipping", _source)

    # ── Tier 5: DeepSeek text-only (absolute last resort) ───────────────
    try:
        from ..models.deepseek import ask_deepseek
        ds = ask_deepseek(message, system=system_prompt)
        if ds and not ds.startswith("["):
            _log(f"✓ DeepSeek text-only responded ({len(ds)} chars)", _source)
            return ds
    except Exception:
        pass

    _log("ALL tiers exhausted — CLI, Gemini, Anthropic API, DeepSeek all failed", _source)
    return (
        "⚠️ **All models currently unavailable.**\n\n"
        "Tried: Claude CLI Pro → Gemini CLI → Anthropic API → DeepSeek\n\n"
        "Likely causes:\n"
        "1. ANTHROPIC_API_KEY invalid or expired (check Railway Variables)\n"
        "2. DeepSeek API key issue\n"
        "3. CLI worker down\n\n"
        "Check the activity log or /credits/pro-status for details."
    )
