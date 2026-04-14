"""
Self-Improvement Agent — Super Agent's autonomous self-repair and evolution layer.

Capabilities:
  1. DIAGNOSE    — scan insight log + DB for failure patterns
  2. READ SELF   — read its own source code from GitHub (super-agent repo)
  3. FIX SELF    — write code fixes back to GitHub → triggers Railway auto-deploy
  4. BUILD ALGO  — generate and commit new algorithms to super-agent-algorithms repo
  5. MANAGE ALL  — access to n8n, Cloudinary, database, Railway, VS Code terminal

Authorization model:
  - SAFE operations (read, diagnose, build algorithms, config tweaks): autonomous
  - CRITICAL operations (modify source code, redeploy, delete data): require safe word
    → agent returns "⚠️ Critical — reply with safe word to authorize" and waits

The agent reports what it did and what it found. It does not silently fail.
"""
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool as lc_tool

from ..config import settings
from .agent_planner import run_with_plan_and_recovery
from ..learning.claude_code_worker import ask_claude_code as _ask_cc


@lc_tool
def ask_claude_code_tool(prompt: str) -> str:
    """
    Ask the in-container Claude Code CLI a question about files in /workspace.
    Claude Code can read actual repo files — use this for code review, bug
    diagnosis, or getting a second opinion on a fix before committing it.
    """
    return _ask_cc(prompt)

# All tools the self-improve agent has access to
from ..tools.shell_tools import (
    run_shell_command,
    run_authorized_shell_command,
    list_workspace,
    run_claude_cli,
    clone_repo,
)
from ..tools.github_tools import (
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_create_branch,
    github_create_pull_request,
)
from ..tools.n8n_tools import (
    n8n_list_workflows,
    n8n_get_workflow,
    n8n_list_executions,
    n8n_get_execution,
    n8n_activate_workflow,
    n8n_deactivate_workflow,
    n8n_create_workflow,
    n8n_update_workflow,
    n8n_execute_workflow,
    n8n_cleanup_test_workflows,
)
from ..tools.railway_tools import (
    railway_list_services,
    railway_get_logs,
    railway_list_variables,
    railway_get_deployment_status,
    railway_redeploy,
    railway_set_variable,
)
from ..tools.database_tools import (
    db_health_check,
    db_list_sessions,
    db_get_error_stats,
    db_get_failure_patterns,
    db_clear_session,
    db_run_safe_query,
)
from ..storage.cloudinary_manager import check_storage, upload_to_storage
from ..tools.search_tools import web_search
from ..tools.algorithm_tools import (
    trigger_algorithm_build,
    list_available_algorithms,
    recommend_model_for_query,
    get_fallback_model,
)
from ..tools.flutter_tools import (
    flutter_create_project,
    flutter_build_apk,
    flutter_test,
    upload_build_artifact,
    flutter_git_push,
)
from ..tools.obsidian_tools import (
    obsidian_list_notes,
    obsidian_read_note,
    obsidian_write_note,
    obsidian_append_to_note,
    obsidian_search_vault,
    obsidian_discover_tools,
    obsidian_call_tool,
)

_SYSTEM = """You are Super Agent's autonomous self-improvement and self-repair engine.

## YOUR PURPOSE
You are the meta-intelligence layer that keeps Super Agent healthy, adaptive, and continuously improving.
You have full access to every system Super Agent depends on.

## CAPABILITIES
- Read and write your own source code (GitHub repo: gelson12/super-agent)
- Trigger Railway redeployment after code fixes
- Build and commit new algorithms to gelson12/super-agent-algorithms
- Inspect and fix the database (sessions, memory, health)
- Manage all n8n workflows (create, update, debug, activate)
- Monitor Railway deployments, logs, and environment variables
- Manage Cloudinary storage (check usage, upload, cleanup)
- Run shell commands in /workspace (clone repos, run tests, inspect files)
- Use Claude CLI for code review and AI-assisted fixes
- Build Flutter mobile apps: scaffold projects, build Android APKs, run tests
- Upload APK/IPA packages to Cloudinary and return download links
- Push Flutter projects to GitHub repos autonomously

## AUTONOMOUS DECISION FRAMEWORK

### SAFE — Act immediately without asking:
- Read anything (source code, logs, DB, Railway status, n8n workflows)
- Diagnose failure patterns from insight log
- Build new algorithms from observed patterns
- Update algorithm files in super-agent-algorithms repo
- Fix typos, missing imports, config values in non-critical files
- Activate/deactivate n8n workflows
- Check Cloudinary storage health
- Run read-only shell commands and DB queries

### CRITICAL — Stop, report, ask for safe word:
- Modify core source files (dispatcher.py, main.py, agents/*.py, models/*.py)
- Trigger Railway redeploy (affects production)
- Set or change Railway environment variables
- Delete database sessions or data
- Delete or overwrite GitHub files in the main super-agent repo
- Any irreversible production action

When CRITICAL: respond with exactly this format:
⚠️ **Critical operation requires authorization**
**Action:** [what you want to do]
**Reason:** [why this fixes the problem]
**Impact:** [what changes and what restarts]
Reply with your safe word to proceed, or say **cancel** to abort.

## SELF-IMPROVEMENT WORKFLOW

When asked to improve/fix/diagnose:
1. Run db_get_failure_patterns() and db_get_error_stats() — understand what's broken
2. Run railway_get_logs() — see recent deployment errors
3. Run db_health_check() — confirm database is healthy
4. Read the relevant source file from GitHub to understand the current code
5. Propose the minimal fix — don't refactor, just fix the specific issue
6. If SAFE: apply immediately and report what you changed
7. If CRITICAL: report the proposed fix and ask for safe word

## ALGORITHM BUILDING
When you identify a recurring pattern not covered by existing algorithms:
1. Use build_algorithms() to trigger the algorithm builder
2. The builder analyses insight log + wisdom store and commits .py files to super-agent-algorithms
3. The algorithm_store auto-loads them within 1 hour (or immediately after store refresh)

## INFRASTRUCTURE AWARENESS — ALWAYS ON

You have live visibility into the ENTIRE Super Agent infrastructure:
- Railway: services, deployment status, logs, environment variables
- n8n: workflow list, execution history, individual execution details
- GitHub: source code, commits, branches, pull requests
- VS Code / code-server: running at port 3001
- Cloudinary: storage usage, uploaded artifacts
- Database: session history, failure patterns, error stats

When the user says ANYTHING like "fix it", "investigate", "find out why", "can you not fix it":
1. IMMEDIATELY use railway_get_logs + railway_get_deployment_status + db_get_failure_patterns
2. Then check the specific failing service (n8n, code-server, uvicorn, etc.)
3. Apply the fix autonomously if SAFE, ask for safe word if CRITICAL
4. Report exactly what you found and what you did — no guessing, no asking for context

## ROUTING & CLASSIFICATION SYSTEM

The dispatcher (`app/routing/dispatcher.py`) routes every user message. Know this architecture so you can diagnose and fix misroutes:

### Keyword routing (zero cost, instant)
- `_GITHUB_KEYWORDS` — set of strings; match → routes to GitHub agent
- `_SHELL_KEYWORDS` — set of strings; match → routes to Shell agent
- `_N8N_KEYWORDS` — set of strings; match → routes to n8n agent
- **Fix pattern:** if a request type is consistently misrouted, add the missing phrase to the correct keyword set.

### Feature 5 — CLI-first classifier (fires when no keyword matches)
`_classify_route_with_confidence(message)` in dispatcher.py — 3-tier cascade:
1. **Claude CLI Pro** (`ask_claude_code`) — subscription, zero extra cost, tried first
2. **Gemini CLI** (`ask_gemini_cli`) — free ~1500 req/day, fallback if CLI Pro fails
3. **Haiku API** (`ask_claude_haiku`) — last resort only, costs tokens
Returns `(CATEGORY, confidence 0.0–1.0)`. Route only applied if confidence ≥ 0.7.

### Operational keyword gate (`app/agents/agent_routing.py`)
`_OPERATIONAL_KEYWORDS["github"|"shell"|"n8n"|"self_improve"]` — if the routed message doesn't match these, the agent runs in text-only tier (no tools). Fix misses by adding the verb to the right list.

### Common fixes you can apply (CRITICAL — need safe word):
- Add keyword to `_GITHUB_KEYWORDS` / `_SHELL_KEYWORDS` / `_N8N_KEYWORDS` in dispatcher.py
- Add verb to `_OPERATIONAL_KEYWORDS[agent_type]` in agent_routing.py
- Lower confidence threshold from 0.7 if too many requests fall through to GENERAL

## REMEMBER
- You are fully autonomous for safe operations — don't ask permission for things you can do safely
- Always report what you found, what you did, and what still needs attention
- Never silently fail — if something is broken, say so clearly
- Prefer minimal targeted fixes over large rewrites
- NEVER tell the user to go to Railway dashboard, n8n UI, or GitHub manually — use your tools
- If in doubt about severity: treat as CRITICAL and ask"""

_SELF_IMPROVE_TOOLS = [
    # Shell
    run_shell_command, run_authorized_shell_command, list_workspace,
    run_claude_cli, clone_repo,
    # Claude Code CLI (reads /workspace files for code review + second opinions)
    ask_claude_code_tool,
    # Web search (current docs, error messages, library releases)
    web_search,
    # GitHub (read own source)
    github_list_repos, github_list_files, github_read_file,
    github_create_or_update_file, github_create_branch, github_create_pull_request,
    # Railway
    railway_list_services, railway_get_logs, railway_list_variables,
    railway_get_deployment_status, railway_redeploy, railway_set_variable,
    # Database
    db_health_check, db_list_sessions, db_get_error_stats,
    db_get_failure_patterns, db_clear_session, db_run_safe_query,
    # n8n
    n8n_list_workflows, n8n_get_workflow, n8n_list_executions,
    n8n_get_execution, n8n_activate_workflow, n8n_deactivate_workflow,
    n8n_create_workflow, n8n_update_workflow, n8n_execute_workflow,
    n8n_cleanup_test_workflows,
    # Cloudinary
    check_storage, upload_to_storage,
    # Algorithms
    trigger_algorithm_build, list_available_algorithms,
    recommend_model_for_query, get_fallback_model,
    # Flutter / Mobile builds
    flutter_create_project, flutter_build_apk, flutter_test,
    upload_build_artifact, flutter_git_push,
    # Obsidian knowledge vault (read/write improvement notes, search prior context)
    obsidian_list_notes, obsidian_read_note, obsidian_write_note,
    obsidian_append_to_note, obsidian_search_vault,
    obsidian_discover_tools, obsidian_call_tool,
]

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=settings.anthropic_api_key,
            max_tokens=8192,
        )
        _agent = create_react_agent(llm, _SELF_IMPROVE_TOOLS)
    return _agent


def _invoke(message: str) -> str:
    try:
        agent = _get_agent()
        result = agent.invoke({
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": message},
            ]
        })
        for msg in reversed(result.get("messages", [])):
            if hasattr(msg, "type") and msg.type in ("ai", "assistant"):
                content = msg.content
                if isinstance(content, list):
                    return " ".join(
                        block.get("text", "") for block in content
                        if isinstance(block, dict)
                    ).strip()
                return str(content).strip()
        return "[Self-improve agent: no response]"
    except Exception as e:
        return f"[Self-improve agent error: {str(e)[:200]}]"


def run_self_improve_agent(message: str, authorized: bool = False) -> str:
    """
    Run the self-improvement agent.
    Routing via shared tiered_agent_invoke:
      - Informational → CLI → Gemini → Anthropic API (LangGraph) → DeepSeek (LangGraph)
      - Operational   → Anthropic API (LangGraph) → DeepSeek (LangGraph)
    """
    from .agent_routing import tiered_agent_invoke
    return tiered_agent_invoke(
        message=message,
        system_prompt=_SYSTEM,
        tools=_SELF_IMPROVE_TOOLS,
        agent_type="self_improve",
        source="self_improve_agent",
    )
