"""
n8n workflow automation tools.

Thin HTTP wrappers over the n8n REST API v1.
All tools require N8N_BASE_URL and N8N_API_KEY to be set as env vars.

Read tools (list, get, get_execution) are always available.
Write tools (create, update, delete, activate, deactivate, execute) require
the owner safe word — enforced by the dispatcher before calling the n8n agent.

RESILIENCE DESIGN:
  - All HTTP calls use exponential-backoff retry (3 attempts: 2s, 5s, 10s)
  - Retries on: connection errors, timeouts, 5xx server errors
  - Never retries on 4xx (client error — won't be fixed by retrying)
  - Generous timeouts: reads 120s, writes 180s, execution polling 300s
  - n8n_list_workflows paginates automatically — no 100-workflow cap
  - n8n_execute_workflow_and_wait polls until completion or 5-min timeout
"""
import json
import time
import httpx
from langchain_core.tools import tool
from ..config import settings
from ..cache.tool_cache import cached_tool

# Timeouts — sized for large workflow builds without interruption
_TIMEOUT_READ   = 120    # reads: list, get workflow, get execution
_TIMEOUT_WRITE  = 180    # writes: create, update, delete, activate
_TIMEOUT_EXEC   = 300    # execution trigger + polling (5 minutes)
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF  = [2, 5, 10]   # seconds between retry attempts


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


def _request(method: str, path: str, body: dict | None = None, timeout: float = _TIMEOUT_READ) -> dict | str:
    """
    HTTP request with exponential-backoff retry on network/server errors.
    4xx errors are returned immediately without retrying.
    """
    err = _check_config()
    if err:
        return err

    url = f"{_base()}{path}"
    last_error = f"[n8n error: all {_RETRY_ATTEMPTS} attempts failed]"

    for attempt in range(_RETRY_ATTEMPTS):
        try:
            if method == "GET":
                r = httpx.get(url, headers=_headers(), timeout=timeout)
            elif method == "POST":
                r = httpx.post(url, headers=_headers(), json=body or {}, timeout=timeout)
            elif method == "PUT":
                r = httpx.put(url, headers=_headers(), json=body or {}, timeout=timeout)
            elif method == "DELETE":
                r = httpx.delete(url, headers=_headers(), timeout=timeout)
                r.raise_for_status()
                return "Deleted successfully."
            else:
                return f"[n8n error: unknown method {method}]"

            if r.status_code >= 500:
                last_error = f"[n8n HTTP {r.status_code}: {r.text[:300]}]"
                if attempt < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF[attempt])
                    continue
                return last_error

            r.raise_for_status()
            return r.json() if r.text.strip() else {"ok": True}

        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_error = f"[n8n network error (attempt {attempt + 1}/{_RETRY_ATTEMPTS}): {e}]"
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF[attempt])
                continue
        except httpx.HTTPStatusError as e:
            return f"[n8n HTTP {e.response.status_code}: {e.response.text[:300]}]"
        except Exception as e:
            return f"[n8n error: {e}]"

    return last_error


def _get(path: str, timeout: float = _TIMEOUT_READ) -> dict | str:
    return _request("GET", path, timeout=timeout)


def _post(path: str, body: dict | None = None, timeout: float = _TIMEOUT_WRITE) -> dict | str:
    return _request("POST", path, body=body, timeout=timeout)


def _put(path: str, body: dict, timeout: float = _TIMEOUT_WRITE) -> dict | str:
    return _request("PUT", path, body=body, timeout=timeout)


def _delete(path: str) -> str:
    result = _request("DELETE", path, timeout=_TIMEOUT_WRITE)
    return result if isinstance(result, str) else "Deleted successfully."


# ── Read tools ─────────────────────────────────────────────────────────────────

@tool
@cached_tool(ttl=60)
def n8n_list_workflows() -> str:
    """
    List ALL n8n workflows with their ID, name, and active/inactive status.
    Paginates automatically — no limit on number of workflows.
    """
    all_rows = []
    cursor = None

    while True:
        path = "/api/v1/workflows?limit=250"
        if cursor:
            path += f"&cursor={cursor}"
        result = _get(path)
        if isinstance(result, str):
            return result
        rows = result.get("data", [])
        all_rows.extend(rows)
        # n8n pagination: nextCursor in response
        cursor = result.get("nextCursor")
        if not cursor:
            break

    if not all_rows:
        return "No workflows found."
    lines = [
        f"{'ACTIVE' if w.get('active') else 'INACTIVE'} | {w['id']} | {w['name']}"
        for w in all_rows
    ]
    return f"Total: {len(all_rows)} workflows\n" + "\n".join(lines)


@tool
@cached_tool(ttl=120)
def n8n_get_workflow(workflow_id: str) -> str:
    """Get the full JSON definition of an n8n workflow by its ID."""
    # Validate: n8n v1+ uses both numeric and alphanumeric workflow IDs
    if not workflow_id or not str(workflow_id).strip():
        return f"[n8n error: invalid workflow ID '{workflow_id}' — must be a non-empty ID]"
    result = _get(f"/api/v1/workflows/{workflow_id.strip()}")
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


@tool
def n8n_list_executions(workflow_id: str = "", limit: int = 25) -> str:
    """
    List recent n8n workflow executions for debugging.
    Pass workflow_id to filter by a specific workflow, or leave empty for all recent.
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
        finished = e.get("stoppedAt", "running")
        lines.append(f"{status.upper()} | exec:{e['id']} | {wf_name} | {started} → {finished}")
    return "\n".join(lines)


@tool
def n8n_get_execution(execution_id: str) -> str:
    """
    Get full details and output of a specific n8n workflow execution.
    Use this to debug failed executions — shows which node failed and the error.
    Uses includeData=true to fetch the full error node details from the n8n API.
    """
    result = _get(f"/api/v1/executions/{execution_id}?includeData=true")
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


# ── Write tools ────────────────────────────────────────────────────────────────

@tool
def n8n_create_workflow(name: str, nodes_json: str, connections_json: str = "{}") -> str:
    """
    Create a new n8n workflow.
    name: display name for the workflow.
    nodes_json: JSON array of node objects.
    connections_json: optional JSON object mapping node connections (default: empty).
    For a minimal webhook trigger workflow, include at least a Webhook node and one action node.
    """
    try:
        nodes = json.loads(nodes_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid nodes_json — {e}]"
    try:
        connections = json.loads(connections_json)
    except json.JSONDecodeError:
        connections = {}

    body = {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": {
            "executionOrder": "v1",
            "saveManualExecutions": True,
            "callerPolicy": "workflowsFromSameOwner",
            "errorWorkflow": "",
        },
    }
    result = _post("/api/v1/workflows", body)
    if isinstance(result, str):
        return result
    wf_id = result.get("id", "")
    wf_name = result.get("name", name)
    success_msg = f"Workflow created: ID={wf_id} name='{wf_name}'"
    try:
        from .n8n_tester import test_workflow as _test
        test_result = _test(wf_id, wf_name)
        if test_result.get("skipped"):
            return success_msg
        if test_result["passed"]:
            return f"{success_msg}\n✅ Auto-test passed (exec {test_result['execution_id']})"
        return (
            f"{success_msg}\n⚠️ Auto-test FAILED: "
            f"nodes={test_result['failed_nodes']}, "
            f"detail={test_result['error_detail'][:200]}"
        )
    except Exception:
        return success_msg


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
    wf_name = result.get("name", workflow_id)
    success_msg = f"Workflow {workflow_id} updated: name='{wf_name}'"
    try:
        from .n8n_tester import test_workflow as _test
        test_result = _test(workflow_id, wf_name)
        if test_result.get("skipped"):
            return success_msg
        if test_result["passed"]:
            return f"{success_msg}\n✅ Auto-test passed (exec {test_result['execution_id']})"
        return (
            f"{success_msg}\n⚠️ Auto-test FAILED: "
            f"nodes={test_result['failed_nodes']}, "
            f"detail={test_result['error_detail'][:200]}"
        )
    except Exception:
        return success_msg


@tool
def n8n_delete_workflow(workflow_id: str) -> str:
    """Delete an n8n workflow by its ID. This action is irreversible."""
    return _delete(f"/api/v1/workflows/{workflow_id}")


@tool
def n8n_cleanup_test_workflows() -> str:
    """
    Delete all test/junk workflows from n8n (Test*, Catch-All*, Watchdog*, etc).
    Protected workflows (Super Agent Chat, Business Hub, Daily Report, etc) are never deleted.
    Returns a summary of what was deleted.
    """
    _JUNK_PREFIXES = (
        "Watchdog-Health-Test", "Watchdog-CLI-Build-Test", "My workflow",
        "Test ", "Test-", "Test_", "Catch-All ",
    )
    _JUNK_EXACT = {
        "Health Monitor - Success Generator",
    }
    _PROTECTED_LOWER = {
        "super agent chat", "business hub", "daily-superagent-report",
        "claude-verification-monitor",
    }

    def _is_junk(name: str) -> bool:
        if name.lower() in _PROTECTED_LOWER:
            return False
        if name in _JUNK_EXACT:
            return True
        if name.startswith(_JUNK_PREFIXES):
            return True
        lower = name.lower()
        return any(p in lower for p in ("(copy)", " copy "))

    # List all workflows
    all_wf = []
    cursor = None
    while True:
        path = "/api/v1/workflows?limit=250"
        if cursor:
            path += f"&cursor={cursor}"
        result = _get(path)
        if isinstance(result, str):
            return f"Failed to list workflows: {result}"
        all_wf.extend(result.get("data", []))
        cursor = result.get("nextCursor")
        if not cursor:
            break

    junk = [w for w in all_wf if _is_junk(w["name"])]
    if not junk:
        return f"No junk workflows found. Total workflows: {len(all_wf)}"

    deleted, errors = [], []
    for w in junk:
        resp = _delete(f"/api/v1/workflows/{w['id']}")
        if isinstance(resp, str) and "error" in resp.lower():
            errors.append(f"{w['name']}: {resp}")
        else:
            deleted.append(w["name"])

    lines = [f"Cleanup complete: {len(deleted)} deleted, {len(errors)} errors."]
    if deleted:
        lines.append("Deleted: " + ", ".join(deleted))
    if errors:
        lines.append("Errors: " + "; ".join(errors))
    lines.append(f"Remaining workflows: {len(all_wf) - len(deleted)}")
    return "\n".join(lines)


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
    Returns the execution ID — use n8n_get_execution to check results.
    """
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid data_json — {e}]"
    result = _post(f"/api/v1/workflows/{workflow_id}/run", {"data": data}, timeout=_TIMEOUT_EXEC)
    if isinstance(result, str):
        return result
    exec_id = result.get("data", {}).get("executionId") or result.get("executionId")
    return f"Workflow {workflow_id} triggered. Execution ID: {exec_id}"


@tool
def n8n_execute_workflow_and_wait(workflow_id: str, data_json: str = "{}", poll_interval: int = 5) -> str:
    """
    Trigger an n8n workflow and WAIT for it to complete, polling every poll_interval seconds.
    Returns the final execution status and output data when finished.
    Useful when you need the result of a workflow before continuing.
    Times out after 5 minutes — for very long workflows use n8n_execute_workflow instead.
    """
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError as e:
        return f"[n8n error: invalid data_json — {e}]"

    # Trigger
    result = _post(f"/api/v1/workflows/{workflow_id}/run", {"data": data}, timeout=_TIMEOUT_EXEC)
    if isinstance(result, str):
        return result

    exec_id = result.get("data", {}).get("executionId") or result.get("executionId")
    if not exec_id:
        return f"[n8n error: no executionId in response — {result}]"

    # Poll until finished or timeout
    deadline = time.time() + _TIMEOUT_EXEC
    while time.time() < deadline:
        time.sleep(poll_interval)
        exec_result = _get(f"/api/v1/executions/{exec_id}?includeData=true")
        if isinstance(exec_result, str):
            return exec_result
        status = exec_result.get("status", "running")
        if status in ("success", "error", "crashed", "canceled"):
            output = exec_result.get("data", {})
            return (
                f"Execution {exec_id} finished: {status.upper()}\n"
                + json.dumps(output, indent=2)[:2000]
            )

    return f"[n8n timeout] Execution {exec_id} still running after {_TIMEOUT_EXEC}s — check with n8n_get_execution."
