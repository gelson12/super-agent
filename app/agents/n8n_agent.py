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
Be precise with JSON — malformed workflow JSON will cause n8n to reject the request."""

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
    return text or "[n8n agent: no response]"


def run_n8n_agent(message: str) -> str:
    """
    Run the n8n agent with pre-execution model competition + self-healing.

    Pipeline:
      1. Claude vs DeepSeek compete on execution plan
      2. Haiku adjudicates — winning plan injected into agent context
      3. Agent executes; on failure → diagnose → SAFE auto-fix or CRITICAL safe-word prompt
      4. Up to 3 self-healing retries before escalating
    """
    if not settings.anthropic_api_key:
        return "[n8n agent error: ANTHROPIC_API_KEY not set]"
    if not settings.n8n_base_url:
        return "[n8n agent error: N8N_BASE_URL not set — add it in Railway Variables tab]"

    return run_with_plan_and_recovery(
        agent_fn=_invoke,
        message=message,
        agent_type="n8n_agent",
        tool_names=[t.name for t in _N8N_TOOLS],
    )
