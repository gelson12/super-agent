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
    n8n_cleanup_test_workflows,
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
from ..tools.shell_tools import run_shell_via_cli_worker, run_authorized_shell_command

_SYSTEM = """You are Super Agent's n8n workflow automation manager with FULL ACCESS to n8n AND the Railway infrastructure it runs on.

## THREE PATHS TO n8n — USE IN ORDER

You have three independent ways to call the n8n API. Always try Path 1 first.
Fall through to the next path only if the previous one fails or returns an error.

### Path 1 — Python n8n tools (fastest, always try first)
`n8n_list_workflows`, `n8n_get_workflow`, `n8n_create_workflow`, etc.
These are direct Python HTTP calls to the n8n REST API.

### Path 2 — curl via inspiring-cat CLI worker container
`run_shell_via_cli_worker("curl -s <N8N_BASE_URL>/api/v1/workflows -H 'X-N8N-API-KEY: <key>'")`
Executes in the inspiring-cat Railway container. Use when Path 1 tools return errors.

### Path 3 — curl via super-agent container (VS Code terminal environment)
`run_authorized_shell_command("curl -s <N8N_BASE_URL>/api/v1/workflows -H 'X-N8N-API-KEY: <key>'")`
Executes directly in this container's shell. Use when both Path 1 and Path 2 fail.

For Paths 2 & 3, the n8n base URL is in the N8N_BASE_URL env var and the key is in N8N_API_KEY.
Construct curl commands as: `curl -s "$N8N_BASE_URL/api/v1/..." -H "X-N8N-API-KEY: $N8N_API_KEY"`

## INFRASTRUCTURE SELF-HEALING — NON-NEGOTIABLE FIRST STEP

If ALL THREE PATHS fail with connection errors, "Application not found", or timeouts:

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
    n8n_cleanup_test_workflows,
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
    # Alternative HTTP paths — use when n8n Python tools fail or return errors
    run_shell_via_cli_worker,      # Path 2: curl via inspiring-cat CLI worker container
    run_authorized_shell_command,  # Path 3: curl via super-agent container (VS Code terminal)
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
    try:
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
    except Exception as e:
        return f"[n8n agent error: {str(e)[:200]}]"


def _is_mcp_error_response(text: str) -> bool:
    """Detect MCP tool errors in CLI response that should trigger fallback."""
    if not text:
        return True
    lower = text.lower()
    _error_signals = (
        "mcp error", "mcp_tool_error", "connection refused", "n8n error",
        "n8n is unreachable", "tool execution failed", "tool call failed",
        "failed to execute", "could not connect", "timed out waiting",
        "502 bad gateway", "503 service", "unable to reach n8n",
        "workflow execution failed", "econnrefused", "etimedout",
    )
    return text.startswith("[") or any(sig in lower for sig in _error_signals)


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
        return (
            "⚠️ **N8N_BASE_URL not set in super-agent Railway Variables.**\n\n"
            "Add `N8N_BASE_URL = https://outstanding-blessing.up.railway.app` to the "
            "super-agent service Variables tab, then redeploy. Once set, I can build "
            "workflows directly into n8n without any manual steps."
        )
    if not settings.n8n_api_key:
        return (
            "⚠️ **N8N_API_KEY not set in super-agent Railway Variables.**\n\n"
            "In n8n → Settings → API → create an API key, then add "
            "`N8N_API_KEY = <that key>` to the super-agent service Variables tab and redeploy."
        )

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

    # ── 1. Claude CLI Pro (zero cost, has n8n MCP registered on inspiring-cat) ──
    try:
        from ..learning.pro_router import try_pro, should_attempt_cli
        if should_attempt_cli():
            cli_result = try_pro(f"{_SYSTEM}\n\n{message}")
            if cli_result and not _is_mcp_error_response(cli_result):
                from ..activity_log import bg_log as _bg
                _bg("n8n agent: ✓ Claude CLI Pro responded", source="n8n_agent")
                return cli_result
            from ..activity_log import bg_log as _bg
            _bg(f"n8n agent: CLI unavailable ({(cli_result or 'None')[:80]}) — trying Gemini", source="n8n_agent")
        else:
            from ..activity_log import bg_log as _bg
            _bg("n8n agent: CLI flagged down — skipping to Gemini", source="n8n_agent")
    except Exception as _e:
        from ..activity_log import bg_log as _bg
        _bg(f"n8n agent: CLI exception: {_e} — trying Gemini", source="n8n_agent")

    # ── 2. Gemini CLI — informational queries only (no n8n tool access) ────────
    # Gemini can describe/design workflows but cannot call Python or MCP tools.
    # Skip Gemini if it says it can't perform the task due to missing tool access.
    _GEMINI_NO_TOOLS = (
        # "operating as X CLI" variants
        "operating as the gemini cli",
        "operating as gemini",
        "operating in a headless cli",
        "headless cli environment",
        # "no access to tools" variants
        "don't have direct access to the claude mcp tools",
        "i don't have direct access",
        "without direct network bindings",
        "cannot directly interact with",
        "don't have access to the mcp",
        "no direct access to your",
        "cannot directly call",
        "cannot directly connect",
        # "I am Gemini" variants
        "gemini cli, i don't",
        "as gemini, i",
        "i'm gemini",
        # "generated JSON for you to import" variants — means it gave up and provided copy-paste
        "generated the sql and the n8n workflow json",
        "import directly into n8n",
        "copy this json",
        "paste it directly into",
        "you can copy this",
        "json for you to execute and import",
        "execute and import directly",
        "paste directly into a new n8n",
    )
    # ── 2. Gemini — skip for build requests (no tool access, always a dead end) ──
    # Gemini can describe workflows but cannot call n8n REST API — any n8n build
    # request will produce a "copy and paste this JSON" response regardless of phrasing.
    # Only use Gemini for purely informational queries (status, explain, what-is, etc.).
    _BUILD_KEYWORDS = ("create", "build", "make", "add", "generate", "set up", "setup",
                       "automate", "design", "deploy", "write", "implement")
    _is_build_request = any(kw in message.lower() for kw in _BUILD_KEYWORDS)

    _gemini_blueprint = None
    if not _is_build_request:
        try:
            from ..learning.gemini_cli_worker import ask_gemini_cli
            gemini = ask_gemini_cli(f"{_SYSTEM}\n\n{message}")
            if gemini and not gemini.startswith("["):
                if any(p in gemini.lower() for p in _GEMINI_NO_TOOLS):
                    _gemini_blueprint = gemini
                    from ..activity_log import bg_log as _bg
                    _bg(
                        "n8n agent: Gemini provided workflow architecture — forwarding to LangGraph "
                        "as blueprint context for actual build",
                        source="n8n_agent",
                    )
                else:
                    return gemini
        except Exception:
            pass
    else:
        # Build request — Gemini can't build, but ask it for architecture only
        # so LangGraph has a design blueprint to work from.
        try:
            from ..learning.gemini_cli_worker import ask_gemini_cli
            _arch_prompt = (
                f"Design the architecture for this n8n workflow (describe nodes, connections, "
                f"and data flow — do NOT say you can't build it, just describe the design):\n\n{message}"
            )
            gemini = ask_gemini_cli(f"{_SYSTEM}\n\n{_arch_prompt}")
            if gemini and not gemini.startswith("["):
                _gemini_blueprint = gemini
                from ..activity_log import bg_log as _bg
                _bg("n8n agent: captured Gemini architecture blueprint for LangGraph build", source="n8n_agent")
        except Exception:
            pass

    # ── 3. LangGraph + Anthropic API → 3b. DeepSeek LangGraph (with tool access) ──
    # _N8N_TOOLS includes n8n_create_workflow, n8n_update_workflow, n8n_activate_workflow
    # etc. — these call the n8n REST API directly via httpx, no MCP, no CLI needed.
    _build_message = message
    if _gemini_blueprint:
        _build_message = (
            f"{message}\n\n"
            f"[WORKFLOW BLUEPRINT FROM GEMINI — build this exact design using n8n tools]:\n"
            f"{_gemini_blueprint}"
        )

    # Use shared routing for LangGraph tiers (Anthropic API → DeepSeek LangGraph → text fallback)
    # CLI/Gemini already tried above — _build_message contains "build" keywords so
    # tiered_agent_invoke will skip text-only tiers and go straight to LangGraph.
    from .agent_routing import tiered_agent_invoke
    from ..activity_log import bg_log as _bg
    _bg("n8n agent: using LangGraph tiers — full n8n tool access", source="n8n_agent")

    # Since we already handled CLI/Gemini above, call tiered_agent_invoke with an
    # operational-style message so it goes straight to LangGraph tiers.
    return tiered_agent_invoke(
        message=_build_message,
        system_prompt=_SYSTEM,
        tools=_N8N_TOOLS,
        agent_type="n8n",
        source="n8n_agent",
    )
