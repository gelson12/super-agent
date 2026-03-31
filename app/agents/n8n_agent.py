"""
n8n Agent — gives Super Agent full access to n8n workflow automation.

Read tools (list, get, inspect executions) are always available.
Write tools (create, update, delete, activate, deactivate, execute) require
the owner safe word — the dispatcher enforces this before calling this agent.
"""
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from ..config import settings
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

_SYSTEM = """You are Super Agent's n8n workflow automation manager with full access to an n8n instance.

You can:
- List all workflows and their active/inactive status
- Read workflow definitions (nodes, connections, settings) in full JSON
- Create new workflows from scratch using n8n node definitions
- Update existing workflows (always read the workflow first to understand its structure)
- Activate or deactivate workflows
- Execute workflows manually with optional input data
- List recent executions and inspect individual execution details for debugging

When creating or updating workflows:
- Always read the existing workflow first with n8n_get_workflow before modifying it
- n8n workflows are JSON with "nodes" (array) and "connections" (object mapping node outputs to inputs)
- Each node has: id, name, type, position [x,y], parameters, and typeVersion
- Common node types: n8n-nodes-base.webhook, n8n-nodes-base.httpRequest,
  n8n-nodes-base.set, n8n-nodes-base.if, n8n-nodes-base.emailSend

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
        _agent = create_react_agent(llm, _N8N_TOOLS)
    return _agent


def run_n8n_agent(message: str) -> str:
    """Run the n8n ReAct agent and return the final text response."""
    if not settings.anthropic_api_key:
        return "[n8n agent error: ANTHROPIC_API_KEY not set]"
    if not settings.n8n_base_url:
        return "[n8n agent error: N8N_BASE_URL not set — add it in Railway Variables tab]"

    agent = _get_agent()
    try:
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
                        block.get("text", "") for block in content if isinstance(block, dict)
                    ).strip()
                return str(content).strip()
        return "[n8n agent: no response]"
    except Exception as e:
        return f"[n8n agent error: {e}]"
