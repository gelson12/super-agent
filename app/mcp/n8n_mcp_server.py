#!/usr/bin/env python3
"""
n8n MCP Server — exposes the full n8n REST API as MCP tools for Claude Code CLI.

Runs as a stdio MCP server. Registered with Claude CLI via:
  claude mcp add n8n --stdio "python /app/mcp/n8n_mcp_server.py"

After registration, every `claude -p "..."` subprocess has direct access to:
  - list_node_types, get_node_type_details   ← discover ALL available nodes
  - search_node_types                         ← find nodes by keyword
  - list_workflows, get_workflow
  - create_workflow, update_workflow, delete_workflow
  - activate_workflow, deactivate_workflow
  - execute_workflow, list_executions, get_execution

Claude Code CLI can now discover every node n8n supports, design the
workflow architecture, and build it — entirely autonomously.

Reads N8N_BASE_URL and N8N_API_KEY from environment variables.
"""
import json
import os
import sys

import requests
from mcp.server.fastmcp import FastMCP

# ── Config ─────────────────────────────────────────────────────────────────────
_BASE_URL = os.environ.get("N8N_BASE_URL", "").rstrip("/")
_API_KEY  = os.environ.get("N8N_API_KEY", "")
_TIMEOUT  = 30

mcp = FastMCP("n8n")


def _headers() -> dict:
    return {
        "X-N8N-API-KEY": _API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _check() -> str | None:
    if not _BASE_URL:
        return "N8N_BASE_URL not set in environment"
    if not _API_KEY:
        return "N8N_API_KEY not set in environment"
    return None


def _get(path: str) -> str:
    err = _check()
    if err:
        return f"[n8n error: {err}]"
    try:
        r = requests.get(f"{_BASE_URL}/api/v1{path}", headers=_headers(), timeout=_TIMEOUT)
        return r.text
    except Exception as e:
        return f"[n8n error: {e}]"


def _post(path: str, body: dict | None = None) -> str:
    err = _check()
    if err:
        return f"[n8n error: {err}]"
    try:
        r = requests.post(
            f"{_BASE_URL}/api/v1{path}",
            headers=_headers(),
            json=body or {},
            timeout=_TIMEOUT,
        )
        return r.text
    except Exception as e:
        return f"[n8n error: {e}]"


def _patch(path: str, body: dict) -> str:
    err = _check()
    if err:
        return f"[n8n error: {err}]"
    try:
        r = requests.patch(
            f"{_BASE_URL}/api/v1{path}",
            headers=_headers(),
            json=body,
            timeout=_TIMEOUT,
        )
        return r.text
    except Exception as e:
        return f"[n8n error: {e}]"


def _put(path: str, body: dict) -> str:
    err = _check()
    if err:
        return f"[n8n error: {err}]"
    try:
        r = requests.put(
            f"{_BASE_URL}/api/v1{path}",
            headers=_headers(),
            json=body,
            timeout=_TIMEOUT,
        )
        return r.text
    except Exception as e:
        return f"[n8n error: {e}]"


def _delete(path: str) -> str:
    err = _check()
    if err:
        return f"[n8n error: {err}]"
    try:
        r = requests.delete(f"{_BASE_URL}/api/v1{path}", headers=_headers(), timeout=_TIMEOUT)
        return r.text
    except Exception as e:
        return f"[n8n error: {e}]"


# ── Tools ──────────────────────────────────────────────────────────────────────

# ── Node discovery (always call these FIRST when designing a workflow) ─────────

@mcp.tool()
def list_node_types(category: str = "") -> str:
    """
    List ALL available n8n node types installed in this instance.

    Call this FIRST before designing any workflow — it tells you exactly
    which nodes are available so you pick real, installed node types
    rather than guessing.

    category: optional filter e.g. "trigger", "action", "transform"
    Returns a JSON array of {name, displayName, description, group, version}.
    """
    result = _get("/node-types")
    if category and not result.startswith("["):
        try:
            data = json.loads(result)
            nodes = data if isinstance(data, list) else data.get("data", [])
            filtered = [
                n for n in nodes
                if category.lower() in " ".join(n.get("group", [])).lower()
                or category.lower() in n.get("displayName", "").lower()
                or category.lower() in n.get("description", "").lower()
            ]
            return json.dumps(filtered)
        except Exception:
            pass
    return result


@mcp.tool()
def get_node_type_details(node_type_name: str) -> str:
    """
    Get the full parameter schema for a specific node type.

    Use this after list_node_types to understand exactly what parameters
    a node accepts before building the workflow JSON.

    node_type_name: e.g. "n8n-nodes-base.microsoftOutlook"
    Returns the full node definition including all configurable parameters.
    """
    # URL-encode the node type name for the path
    encoded = requests.utils.quote(node_type_name, safe="")
    return _get(f"/node-types/{encoded}")


@mcp.tool()
def search_node_types(keyword: str) -> str:
    """
    Search available n8n nodes by keyword.

    Searches across node name, display name, and description.
    Use this to find the right node when you know what service or
    action you need (e.g. "outlook", "slack", "postgres", "cron").

    Returns matching nodes with their type names and descriptions.
    """
    result = _get("/node-types")
    if result.startswith("["):
        return result
    try:
        data = json.loads(result)
        nodes = data if isinstance(data, list) else data.get("data", [])
        kw = keyword.lower()
        matches = [
            {
                "name": n.get("name"),
                "displayName": n.get("displayName"),
                "description": n.get("description", "")[:120],
                "group": n.get("group", []),
                "version": n.get("defaultVersion", n.get("version", 1)),
            }
            for n in nodes
            if kw in n.get("name", "").lower()
            or kw in n.get("displayName", "").lower()
            or kw in n.get("description", "").lower()
        ]
        return json.dumps({"keyword": keyword, "count": len(matches), "nodes": matches})
    except Exception as e:
        return f"[n8n error: search failed — {e}]"


# ── Workflow CRUD ──────────────────────────────────────────────────────────────

@mcp.tool()
def list_workflows(active_only: bool = False) -> str:
    """List all n8n workflows. Set active_only=true to see only active ones."""
    result = _get("/workflows?limit=100")
    if active_only and not result.startswith("["):
        try:
            data = json.loads(result)
            active = [w for w in data.get("data", []) if w.get("active")]
            return json.dumps({"data": active, "count": len(active)})
        except Exception:
            pass
    return result


@mcp.tool()
def get_workflow(workflow_id: str) -> str:
    """Get full details of a workflow including its nodes and connections."""
    return _get(f"/workflows/{workflow_id}")


@mcp.tool()
def create_workflow(workflow_json: str) -> str:
    """
    Create a new n8n workflow.

    workflow_json must be a JSON string with this structure:
    {
      "name": "My Workflow",
      "nodes": [...],
      "connections": {...},
      "settings": {"executionOrder": "v1"}
    }

    Each node: {"id": "uuid", "name": "str", "type": "n8n-nodes-base.xxx",
                 "position": [x, y], "parameters": {...}, "typeVersion": 1}

    Returns the created workflow with its assigned ID.
    """
    try:
        body = json.loads(workflow_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid JSON — {e}]"
    return _post("/workflows", body)


@mcp.tool()
def update_workflow(workflow_id: str, workflow_json: str) -> str:
    """
    Update an existing workflow. Always call get_workflow first to get
    the current structure, then modify it and pass the full updated JSON.

    workflow_json: full workflow JSON string (same format as create_workflow).
    """
    try:
        body = json.loads(workflow_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid JSON — {e}]"
    return _put(f"/workflows/{workflow_id}", body)


@mcp.tool()
def delete_workflow(workflow_id: str) -> str:
    """Delete a workflow permanently by ID."""
    return _delete(f"/workflows/{workflow_id}")


@mcp.tool()
def activate_workflow(workflow_id: str) -> str:
    """Activate a workflow so it responds to triggers."""
    return _patch(f"/workflows/{workflow_id}", {"active": True})


@mcp.tool()
def deactivate_workflow(workflow_id: str) -> str:
    """Deactivate a workflow (stops it from responding to triggers)."""
    return _patch(f"/workflows/{workflow_id}", {"active": False})


@mcp.tool()
def execute_workflow(workflow_id: str, input_data: str = "{}") -> str:
    """
    Manually trigger a workflow execution.

    input_data: JSON string of input data to pass to the workflow.
    Example: '{"name": "Alice", "email": "alice@example.com"}'
    """
    try:
        data = json.loads(input_data)
    except json.JSONDecodeError:
        data = {}
    return _post(f"/workflows/{workflow_id}/run", {"workflowData": data})


@mcp.tool()
def list_executions(workflow_id: str = "", limit: int = 20, status: str = "") -> str:
    """
    List recent workflow executions.

    workflow_id: filter by specific workflow (optional)
    limit: number of results (default 20, max 100)
    status: filter by "success", "error", "waiting" (optional)
    """
    params = [f"limit={min(limit, 100)}"]
    if workflow_id:
        params.append(f"workflowId={workflow_id}")
    if status:
        params.append(f"status={status}")
    return _get(f"/executions?{'&'.join(params)}")


@mcp.tool()
def get_execution(execution_id: str) -> str:
    """
    Get full details of a specific execution including node results and errors.
    Use this to debug failed workflows — it shows exactly which node failed and why.
    """
    return _get(f"/executions/{execution_id}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
