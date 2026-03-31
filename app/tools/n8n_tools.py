"""
n8n workflow automation tools.

Thin HTTP wrappers over the n8n REST API v1.
All tools require N8N_BASE_URL and N8N_API_KEY to be set as env vars.

Read tools (list, get, get_execution) are always available.
Write tools (create, update, delete, activate, deactivate, execute) require
the owner safe word — enforced by the dispatcher before calling the n8n agent.
"""
import json
import httpx
from langchain_core.tools import tool
from ..config import settings
from ..cache.tool_cache import cached_tool

_TIMEOUT = 30


def _headers() -> dict:
    return {
        "X-N8N-API-KEY": settings.n8n_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _base() -> str:
    return settings.n8n_base_url.rstrip("/")


def _check_config() -> str | None:
    """Return an error string if config is missing, else None."""
    if not settings.n8n_base_url:
        return "[n8n error: N8N_BASE_URL not set — add it in Railway Variables]"
    if not settings.n8n_api_key:
        return "[n8n error: N8N_API_KEY not set — add it in Railway Variables]"
    return None


def _get(path: str) -> dict | str:
    err = _check_config()
    if err:
        return err
    try:
        r = httpx.get(f"{_base()}{path}", headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        return f"[n8n HTTP {e.response.status_code}: {e.response.text}]"
    except Exception as e:
        return f"[n8n error: {e}]"


def _post(path: str, body: dict | None = None) -> dict | str:
    err = _check_config()
    if err:
        return err
    try:
        r = httpx.post(
            f"{_base()}{path}",
            headers=_headers(),
            json=body or {},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json() if r.text else {"ok": True}
    except httpx.HTTPStatusError as e:
        return f"[n8n HTTP {e.response.status_code}: {e.response.text}]"
    except Exception as e:
        return f"[n8n error: {e}]"


def _put(path: str, body: dict) -> dict | str:
    err = _check_config()
    if err:
        return err
    try:
        r = httpx.put(
            f"{_base()}{path}",
            headers=_headers(),
            json=body,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        return f"[n8n HTTP {e.response.status_code}: {e.response.text}]"
    except Exception as e:
        return f"[n8n error: {e}]"


def _delete(path: str) -> str:
    err = _check_config()
    if err:
        return err
    try:
        r = httpx.delete(f"{_base()}{path}", headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return "Deleted successfully."
    except httpx.HTTPStatusError as e:
        return f"[n8n HTTP {e.response.status_code}: {e.response.text}]"
    except Exception as e:
        return f"[n8n error: {e}]"


# ── Read tools ─────────────────────────────────────────────────────────────────

@tool
@cached_tool(ttl=60)
def n8n_list_workflows() -> str:
    """List all n8n workflows with their ID, name, and active/inactive status."""
    result = _get("/api/v1/workflows?limit=100")
    if isinstance(result, str):
        return result
    rows = result.get("data", [])
    if not rows:
        return "No workflows found."
    lines = [f"{'ACTIVE' if w.get('active') else 'INACTIVE'} | {w['id']} | {w['name']}" for w in rows]
    return "\n".join(lines)


@tool
@cached_tool(ttl=120)
def n8n_get_workflow(workflow_id: str) -> str:
    """Get the full JSON definition of an n8n workflow by its ID."""
    result = _get(f"/api/v1/workflows/{workflow_id}")
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


@tool
def n8n_list_executions(workflow_id: str = "", limit: int = 10) -> str:
    """
    List recent n8n workflow executions for debugging.
    Pass workflow_id to filter by a specific workflow, or leave empty for all recent executions.
    """
    path = f"/api/v1/executions?limit={limit}"
    if workflow_id:
        path += f"&workflowId={workflow_id}"
    result = _get(path)
    if isinstance(result, str):
        return result
    rows = result.get("data", [])
    if not rows:
        return "No executions found."
    lines = []
    for e in rows:
        status = e.get("status", "unknown")
        wf_name = e.get("workflowData", {}).get("name", "?")
        started = e.get("startedAt", "?")
        lines.append(f"{status.upper()} | exec:{e['id']} | workflow:{wf_name} | started:{started}")
    return "\n".join(lines)


@tool
def n8n_get_execution(execution_id: str) -> str:
    """
    Get full details and output of a specific n8n workflow execution.
    Use this to debug failed executions — shows which node failed and the error.
    """
    result = _get(f"/api/v1/executions/{execution_id}")
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


# ── Write tools ────────────────────────────────────────────────────────────────

@tool
def n8n_create_workflow(name: str, nodes_json: str) -> str:
    """
    Create a new n8n workflow.
    name: display name for the workflow.
    nodes_json: JSON array of node objects. For a minimal webhook trigger workflow,
    include at least a Start/Webhook node and one action node.
    """
    try:
        nodes = json.loads(nodes_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid nodes_json — {e}]"
    body = {
        "name": name,
        "nodes": nodes,
        "connections": {},
        "settings": {"executionOrder": "v1"},
    }
    result = _post("/api/v1/workflows", body)
    if isinstance(result, str):
        return result
    return f"Workflow created: ID={result.get('id')} name='{result.get('name')}'"


@tool
def n8n_update_workflow(workflow_id: str, workflow_json: str) -> str:
    """
    Update an existing n8n workflow with a new full definition.
    workflow_json: the complete updated workflow JSON (get it first with n8n_get_workflow).
    """
    try:
        body = json.loads(workflow_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid workflow_json — {e}]"
    result = _put(f"/api/v1/workflows/{workflow_id}", body)
    if isinstance(result, str):
        return result
    return f"Workflow {workflow_id} updated: name='{result.get('name')}'"


@tool
def n8n_delete_workflow(workflow_id: str) -> str:
    """Delete an n8n workflow by its ID. This action is irreversible."""
    return _delete(f"/api/v1/workflows/{workflow_id}")


@tool
def n8n_activate_workflow(workflow_id: str) -> str:
    """Activate an n8n workflow so it runs automatically on its triggers."""
    result = _post(f"/api/v1/workflows/{workflow_id}/activate")
    if isinstance(result, str):
        return result
    return f"Workflow {workflow_id} activated."


@tool
def n8n_deactivate_workflow(workflow_id: str) -> str:
    """Deactivate an n8n workflow to stop it from running on its triggers."""
    result = _post(f"/api/v1/workflows/{workflow_id}/deactivate")
    if isinstance(result, str):
        return result
    return f"Workflow {workflow_id} deactivated."


@tool
def n8n_execute_workflow(workflow_id: str, data_json: str = "{}") -> str:
    """
    Manually trigger an n8n workflow execution.
    data_json: optional JSON object of input data to pass to the workflow.
    Returns the execution ID for tracking with n8n_get_execution.
    """
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid data_json — {e}]"
    result = _post(f"/api/v1/workflows/{workflow_id}/run", {"data": data})
    if isinstance(result, str):
        return result
    exec_id = result.get("data", {}).get("executionId") or result.get("executionId")
    return f"Workflow {workflow_id} triggered. Execution ID: {exec_id}"
