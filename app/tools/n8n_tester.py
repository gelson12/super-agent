"""
n8n Workflow Auto-Tester — runs a smoke-test execution after every create/update.

After n8n_create_workflow or n8n_update_workflow succeeds, this module:
  1. Activates the workflow (if inactive)
  2. Triggers an execution with an empty payload
  3. Polls until completion (up to 60s)
  4. Parses the result for failed nodes
  5. Deactivates the workflow if the test failed
  6. Persists the result to /workspace/n8n_test_results.json

Storage: /workspace/n8n_test_results.json  (fallback ./)
Format:  flat JSON array, capped at 300 entries
Writes:  best-effort / exception-swallowed — never block the tool response
"""
import json
import os
import time
import datetime
from pathlib import Path

_TEST_LOG_FILE = "n8n_test_results.json"
_MAX_RESULTS = 300
_POLL_INTERVAL = 5      # seconds between status polls
_POLL_TIMEOUT = 60      # max seconds to wait for test execution


def _resolve_path() -> Path:
    base = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
    return base / _TEST_LOG_FILE


def _load() -> list:
    try:
        return json.loads(_resolve_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list) -> None:
    try:
        _resolve_path().write_text(
            json.dumps(entries[-_MAX_RESULTS:], indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _append_result(result: dict) -> None:
    entries = _load()
    entries.append(result)
    _save(entries)


def test_workflow(workflow_id: str, workflow_name: str = "") -> dict:
    """
    Smoke-test a workflow immediately after create/update.

    Returns a structured result dict:
    {
        "passed": bool,
        "workflow_id": str,
        "workflow_name": str,
        "execution_id": str | None,
        "failed_nodes": list[str],
        "error_detail": str,
        "tested_at": ISO str,
        "deactivated_on_fail": bool,
        "skipped": bool,      # True if n8n not configured or test not applicable
        "skip_reason": str,
    }
    """
    tested_at = datetime.datetime.utcnow().isoformat()
    base_result = {
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "tested_at": tested_at,
        "execution_id": None,
        "failed_nodes": [],
        "error_detail": "",
        "deactivated_on_fail": False,
        "skipped": False,
        "skip_reason": "",
    }

    try:
        # Import here to avoid circular imports; n8n_tools is in the same package
        from .n8n_tools import (
            n8n_activate_workflow,
            n8n_execute_workflow,
            n8n_get_execution,
            n8n_deactivate_workflow,
        )
        from ..config import settings

        if not settings.n8n_base_url or not settings.n8n_api_key:
            result = {**base_result, "passed": True, "skipped": True,
                      "skip_reason": "n8n not configured"}
            _append_result(result)
            return result

        # Step 1: activate (best-effort — some webhooks need to be active to run)
        try:
            n8n_activate_workflow.invoke({"workflow_id": workflow_id})
        except Exception:
            pass

        # Step 2: trigger execution with empty payload
        exec_response = n8n_execute_workflow.invoke(
            {"workflow_id": workflow_id, "data_json": "{}"}
        )
        if isinstance(exec_response, str) and exec_response.startswith("["):
            result = {**base_result, "passed": False,
                      "error_detail": exec_response[:300]}
            _append_result(result)
            return result

        # Extract execution ID from response string e.g. "... Execution ID: 123"
        exec_id = None
        if isinstance(exec_response, str) and "Execution ID:" in exec_response:
            try:
                exec_id = exec_response.split("Execution ID:")[-1].strip().split()[0]
            except Exception:
                pass

        if not exec_id:
            result = {**base_result, "passed": False,
                      "error_detail": f"could not parse execution ID from: {exec_response[:200]}"}
            _append_result(result)
            return result

        # Step 3: poll for completion
        deadline = time.time() + _POLL_TIMEOUT
        status = "running"
        exec_data: dict = {}
        while time.time() < deadline:
            time.sleep(_POLL_INTERVAL)
            raw = n8n_get_execution.invoke({"execution_id": exec_id})
            if isinstance(raw, str) and not raw.startswith("["):
                try:
                    exec_data = json.loads(raw)
                    status = exec_data.get("status", "running")
                    if status in ("success", "error", "crashed", "canceled"):
                        break
                except json.JSONDecodeError:
                    pass

        # Step 4: parse result
        failed_nodes: list[str] = []
        error_detail = ""

        if status == "success":
            passed = True
        elif status == "running":
            passed = False
            error_detail = f"test timed out after {_POLL_TIMEOUT}s — execution still running"
        else:
            passed = False
            # Try to extract failed node names from execution data
            try:
                run_data = exec_data.get("data", {}).get("resultData", {}).get("runData", {})
                for node_name, node_runs in run_data.items():
                    for run in (node_runs or []):
                        if run.get("error"):
                            failed_nodes.append(node_name)
                            err_msg = run["error"].get("message", "")
                            if err_msg and not error_detail:
                                error_detail = f"{node_name}: {err_msg[:200]}"
            except Exception:
                pass
            if not error_detail:
                error_detail = f"execution status: {status}"

        # Step 5: deactivate on failure
        deactivated = False
        if not passed:
            try:
                n8n_deactivate_workflow.invoke({"workflow_id": workflow_id})
                deactivated = True
            except Exception:
                pass

        result = {
            **base_result,
            "passed": passed,
            "execution_id": exec_id,
            "failed_nodes": failed_nodes,
            "error_detail": error_detail,
            "deactivated_on_fail": deactivated,
        }
        _append_result(result)
        return result

    except Exception as e:
        result = {**base_result, "passed": False, "error_detail": f"tester exception: {e}"}
        try:
            _append_result(result)
        except Exception:
            pass
        return result


def get_test_results(n: int = 20) -> list[dict]:
    """Return the last N test results, newest first."""
    try:
        return list(reversed(_load()))[:n]
    except Exception:
        return []
