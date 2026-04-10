from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from ..config import settings
from .agent_planner import run_with_plan_and_recovery, extract_final_agent_text
from ..tools.github_tools import (
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
)
from ..tools.railway_tools import (
    railway_list_variables,
    railway_get_logs,
    railway_get_deployment_status,
)
from ..tools.shell_tools import run_shell_command

_SYSTEM = """You are a GitHub assistant with LIVE access to Gelson's GitHub account (gelson12).

EXECUTION STANCE: Execute immediately. Never say 'I don't have access' — GITHUB_PAT is configured and live.

## SELF-HEALING — MANDATORY WHEN ERRORS OCCUR

If any GitHub tool returns an error (auth failure, rate limit, 404, network error):

1. AUTH FAILURES ("401 Bad credentials", "Bad PAT"):
   → Call railway_list_variables — confirm GITHUB_PAT is set in Railway environment
   → Call railway_get_logs — check if the PAT was recently rotated or expired
   → Report the exact variable state found; never just say "check your PAT"

2. RATE LIMIT ("403 rate limit exceeded"):
   → Call run_shell_command with "date" to get current UTC time
   → Report when the rate limit resets (GitHub rate limit resets every hour)
   → Queue the remaining tasks for retry

3. REPO/FILE NOT FOUND ("404"):
   → Call github_list_repos first to discover what repos actually exist
   → Try alternate branch names: main → master → develop
   → Never give up after a single 404 — adapt and retry

4. NETWORK/TIMEOUT errors:
   → Call railway_get_deployment_status to check if the container itself is healthy
   → Call railway_get_logs to see recent errors
   → Retry the operation once before escalating

NEVER tell the user to manually check GitHub, rotate a PAT, or go to any dashboard.
Use your tools to investigate first, fix what you can, and report exactly what you found.

## REPO DISCOVERY
If the user does not specify a repo name, call github_list_repos first to discover
all available repos, then choose the most relevant one based on the task context.

You can:
- List all repositories under gelson12 (use this when repo name is unknown)
- Read any file in any repo
- Create, update, or delete files (with a commit message)
- Create branches
- Open pull requests

Always confirm the exact action taken and its result. Use clear, descriptive commit messages.
When unsure of a branch name, try 'main' first then 'master'.
Never guess file content — read the file first if you need to modify it."""

_GITHUB_TOOLS = [
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
    # Self-healing tools — used when GitHub errors occur
    railway_list_variables,
    railway_get_logs,
    railway_get_deployment_status,
    run_shell_command,
]

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=settings.anthropic_api_key,
            max_tokens=settings.max_tokens_claude,
        )
        _agent = create_react_agent(llm, _GITHUB_TOOLS)
    return _agent


def _invoke(message: str) -> str:
    """Raw agent invoke via LangGraph + Anthropic API (last resort)."""
    agent = _get_agent()
    result = agent.invoke({
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": message},
        ]
    })
    text = extract_final_agent_text(result)
    return text or "[GitHub agent: no response]"


def run_github_agent(message: str) -> str:
    """
    Run the GitHub agent. Routing: Claude CLI → Gemini → Anthropic API (last resort).
    """
    if not settings.github_pat:
        return "[GitHub agent error: GITHUB_PAT not set]"

    # ── 1→2→3→4: CLI Pro → Gemini → Anthropic API (with tools) → DeepSeek ─────
    # Tier 1: Claude CLI Pro (free)
    try:
        from ..learning.pro_router import try_pro, should_attempt_cli
        if should_attempt_cli():
            cli_result = try_pro(f"{_SYSTEM}\n\n{message}")
            if cli_result and not cli_result.startswith("["):
                return cli_result
    except Exception:
        pass

    # Tier 2: Gemini CLI (free)
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(f"{_SYSTEM}\n\n{message}")
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass

    # Tier 3: LangGraph + Anthropic API — full github_tools
    try:
        from ..activity_log import bg_log as _bg
        _bg("GitHub agent: using LangGraph (Anthropic API) — CLI/Gemini unavailable", source="github_agent")
        return _invoke(message)
    except Exception as _e:
        if "credit" not in str(_e).lower():
            return f"⚠️ GitHub agent error: {_e}"

    # Tier 4: DeepSeek (last resort — text only, no GitHub tools)
    try:
        from ..models.deepseek import ask_deepseek
        ds = ask_deepseek(message, system=_SYSTEM)
        if ds and not ds.startswith("["):
            return ds
    except Exception:
        pass

    return "⚠️ All response tiers unavailable (CLI, Gemini, Anthropic, DeepSeek). Please retry in a moment."
