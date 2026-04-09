"""
n8n Agent — gives Super Agent full access to n8n workflow automation.

Read tools (list, get, inspect executions) are always available.
Write tools (create, update, delete, activate, deactivate, execute) require
the owner safe word — the dispatcher enforces this before calling this agent.
"""
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from ..config import settings
from .agent_planner import run_with_plan_and_recovery, extract_final_agent_text
from ..tools.n8n_tools import (
    n8n_list_workflows,
    n8n_get_workflow,
    n8n_create_workflow,
    n8n_update_workflow,
    n8n_delete_workflow,
    n8n_activate_workflow,
    n8n_deactivate_workflow,
    n8n_execute_workflow,
    n8n_list_executions,
    n8n_get_execution,
)
from ..tools.railway_tools import (
    railway_list_services,
    railway_get_logs,
    railway_get_deployment_status,
    railway_list_variables,
    railway_redeploy,
)

_SYSTEM = """You are Super Agent's n8n workflow automation manager with FULL ACCESS to n8n AND the Railway infrastructure it runs on.

## INFRASTRUCTURE SELF-HEALING — NON-NEGOTIABLE FIRST STEP

If ANY n8n tool call returns 404, "Application not found", connection refused, timeout, or network error:

STOP. DO NOT give the user manual instructions like "go to Railway dashboard". YOU ARE the infrastructure manager. Investigate and fix it yourself:

1. `railway_get_deployment_status` → is the n8n service running?
2. `railway_list_services` → confirm the n8n service exists and note its URL
3. `railway_get_logs` → scan for crash errors, OOM kills, startup failures
4. `railway_list_variables` → confirm N8N_BASE_URL matches the running service URL
5. If the service is crashed/stopped: attempt `railway_redeploy` to restart it
6. After redeploy, wait 20s then retry the original n8n operation
7. Only escalate to the user AFTER you have exhausted all autonomous fixes, and only tell them WHAT you found and what SPECIFIC action you took

YOU HAVE LIVE ACCESS TO RAILWAY. Never tell a user to "go to the Railway dashboard" — check it yourself.

## WORKFLOW OPERATIONS

You can:
- List all workflows and their active/inactive status
- Read workflow definitions (nodes, connections, settings) in full JSON
- Create new workflows from scratch using n8n node definitions
- Update existing workflows (always read the workflow first to understand its structure)
- Activate or deactivate workflows
- Execute workflows manually with optional input data
- List recent executions and inspect individual execution details for debugging

## ADAPTIVE EXECUTION — CRITICAL

When a task is large or complex (e.g., creating a workflow with many nodes):
1. NEVER attempt to build everything in one call — break it into phases
2. Phase 1: Create a minimal skeleton (Webhook trigger + Switch node only) → call n8n_create_workflow
3. Phase 2: Read back the created workflow with n8n_get_workflow to get its ID and structure
4. Phase 3: Add service groups incrementally using n8n_update_workflow (5 nodes at a time max)
5. Confirm success after each phase before proceeding to the next

If any phase fails:
- Analyse the error message returned by the tool
- Adjust the JSON (fix syntax, missing fields, wrong node type) and retry
- If the workflow was partially created, read it first with n8n_get_workflow before updating
- Never give up after a single failure — adapt, investigate infrastructure if needed, and retry

## WORKFLOW JSON RULES

- n8n workflows are JSON with "nodes" (array) and "connections" (object mapping node outputs to inputs)
- Each node has: id (string), name (string), type (string), position ([x,y]), parameters (object), typeVersion (number)
- Common node types: n8n-nodes-base.webhook, n8n-nodes-base.httpRequest,
  n8n-nodes-base.switch, n8n-nodes-base.set, n8n-nodes-base.if, n8n-nodes-base.emailSend
- Switch node routes on a field value: parameters.rules.values is an array of {value, outputKey} pairs
- Connections format: {"NodeName": {"main": [[{"node": "TargetNode", "type": "main", "index": 0}]]}}

When debugging failed executions:
1. List recent executions to find the failed one
2. Get the full execution details — it shows which node failed and the exact error
3. Propose a fix to the workflow
4. Apply the fix if authorized

Always confirm the workflow ID and name before any write operation.
Be precise with JSON — malformed workflow JSON will cause n8n to reject the request.

## BUILDING WORKFLOWS FROM NATURAL LANGUAGE

When the user describes what they want in plain English (not technical JSON):

1. **Extract the three parts:**
   - TRIGGER — what starts the workflow (time schedule, webhook, form submit, etc.)
   - ACTIONS — what it does step by step
   - OUTPUT — where the result goes (email, Slack, spreadsheet, HTTP response, etc.)

2. **Map to n8n node types:**
   - "every day / hour / week at X" → `n8n-nodes-base.scheduleTrigger`
   - "when a webhook fires / HTTP request" → `n8n-nodes-base.webhook`
   - "send an email" → `n8n-nodes-base.emailSend`
   - "post to Slack" → `n8n-nodes-base.slack`
   - "save to Google Sheets" → `n8n-nodes-base.googleSheets`
   - "call an API / HTTP request" → `n8n-nodes-base.httpRequest`
   - "ask AI / summarise / analyse" → `n8n-nodes-base.httpRequest` POST to `https://super-agent-production.up.railway.app/chat` with body `{"message": "{{input}}", "session_id": "n8n-auto"}`
   - "if / filter / condition" → `n8n-nodes-base.if`
   - "transform / set fields" → `n8n-nodes-base.set`
   - "wait / delay" → `n8n-nodes-base.wait`

3. **Build in phases — never attempt everything in one call:**
   - Phase 1: `n8n_create_workflow` with trigger node + first action node only
   - Phase 2: `n8n_get_workflow` to confirm creation and get the live ID
   - Phase 3: `n8n_update_workflow` to add remaining nodes (max 5 per update)
   - Phase 4: `n8n_activate_workflow` to make it live
   - Report: workflow name, ID, what it does, and its webhook URL if applicable

4. **NEVER refuse a natural language request.** If you're unsure of the exact node type, use `n8n-nodes-base.httpRequest` as a universal fallback — it can call any API.

5. **For AI steps inside workflows:** Always use an HTTP Request node pointing at Super Agent (`https://super-agent-production.up.railway.app/chat`) rather than a direct Anthropic node. Super Agent handles routing, memory, and all models in one call."""

_N8N_TOOLS = [
    n8n_list_workflows,
    n8n_get_workflow,
    n8n_create_workflow,
    n8n_update_workflow,
    n8n_delete_workflow,
    n8n_activate_workflow,
    n8n_deactivate_workflow,
    n8n_execute_workflow,
    n8n_list_executions,
    n8n_get_execution,
    # Railway infrastructure tools — used for self-healing when n8n is unreachable
    railway_list_services,
    railway_get_logs,
    railway_get_deployment_status,
    railway_list_variables,
    railway_redeploy,
]

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=settings.anthropic_api_key,
            max_tokens=8192,  # n8n workflows are large JSON — needs room to think + build
        )
        _agent = create_react_agent(llm, _N8N_TOOLS)
    return _agent


def _invoke(message: str) -> str:
    """Raw agent invoke — called by run_with_plan_and_recovery."""
    agent = _get_agent()
    result = agent.invoke({
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": message},
        ]
    })
    text = extract_final_agent_text(result)
    # Track n8n agent API cost separately so /credits/breakdown shows it clearly
    try:
        from ..learning.cost_ledger import record_call as _rc
        _rc("CLAUDE", len(message), len(text or ""), category="n8n_build")
    except Exception:
        pass
    return text or "[n8n agent: no response]"


def run_n8n_agent(message: str) -> str:
    """
    Run the n8n agent with pre-execution model competition + self-healing.

    Routing (cheapest first):
      1. Claude CLI on inspiring-cat — has n8n MCP registered, zero API cost, full tool access
      2. Gemini CLI — text-only, no tool access; used only for informational n8n questions
      3. LangGraph + Anthropic API — last resort, full tool access via Python n8n_tools

    Pre-flight: verify n8n is reachable; auto-repair if not (restart Railway, check vars)
    """
    if not settings.n8n_base_url:
        return "[n8n agent error: N8N_BASE_URL not set — add it in Railway Variables tab]"

    # ── Pre-flight: verify n8n is reachable before handing to the agent ──────
    from ..tools.n8n_repair import attempt_n8n_repair, n8n_health_check
    health = n8n_health_check()
    if not health["reachable"]:
        issues = health["issues"]
        error_str = issues[0] if issues else "n8n unreachable"
        fixed, fixes = attempt_n8n_repair(error_str)
        if fixed:
            health2 = n8n_health_check()
            if not health2["reachable"]:
                return (
                    f"⚠️ n8n is unreachable. Auto-repair was attempted:\n"
                    + "\n".join(f"• {f}" for f in fixes)
                    + "\n\nService is still not responding. Check Railway logs for crash details."
                )
            message = (
                f"[AUTO-REPAIR APPLIED before your request]\n"
                + "\n".join(f"• {f}" for f in fixes)
                + f"\n\nn8n is now reachable. Proceeding with your request:\n{message}"
            )
        else:
            return (
                f"⚠️ n8n is unreachable and no auto-fix could be applied.\n"
                f"Error: {error_str}\n\nInvestigating Railway infrastructure now..."
            )

    # ── 1. Claude CLI (inspiring-cat has n8n MCP registered — zero API cost) ──
    # Claude CLI on inspiring-cat runs `claude mcp add n8n` at boot, so it has
    # FULL n8n tool access (create, update, activate workflows) at zero credit cost.
    try:
        from ..learning.pro_router import try_pro, is_pro_available
        if is_pro_available():
            cli_result = try_pro(f"{_SYSTEM}\n\n{message}")
            if cli_result and not cli_result.startswith("["):
                return cli_result
    except Exception:
        pass

    # ── 2. Gemini CLI — try for ALL requests (free, no API cost) ────────────
    # For build tasks Gemini can't call Python tools, but it can design/describe
    # the workflow. Always try it before burning Anthropic API credits.
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(f"{_SYSTEM}\n\n{message}")
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass

    # ── 3. CLI and Gemini both unavailable — never call Anthropic API ───────
    # API credits are reserved for conversational ask_claude() calls only.
    # Agent tool-use (create/update workflows) requires CLI access — cannot
    # substitute with a raw API call that has no n8n tool bindings.
    return (
        "⚠️ Claude CLI (inspiring-cat) and Gemini are both temporarily unavailable.\n\n"
        "Cannot build or modify n8n workflows without CLI tool access. "
        "Please try again in a few minutes — the CLI worker may be busy or restarting.\n\n"
        "If this persists, open inspiring-cat VS Code and run `claude login` to refresh credentials."
    )
