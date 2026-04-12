"""
n8n autonomous repair module — mirrors build_repair.py for the n8n layer.

When n8n operations fail, call attempt_n8n_repair(error, context) to apply
known fixes automatically before escalating to the user.

Also exposes:
  - n8n_health_check()     — full n8n health snapshot (reachability, workflows, executions)
  - _monitor_n8n_health()  — called by the scheduler every 15 minutes
"""
import json
import time
from pathlib import Path
from ..config import settings


# ── Shared log ────────────────────────────────────────────────────────────────
N8N_HEALTH_LOG = Path("/workspace/n8n_health.log")


def _log(msg: str) -> None:
    from datetime import datetime
    line = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}\n"
    try:
        with N8N_HEALTH_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ── Known error patterns → fix functions ─────────────────────────────────────

def _fix_restart_n8n(error: str, context: dict) -> str | None:
    """n8n service is down — trigger Railway redeploy to restart it."""
    from ..tools.railway_tools import railway_redeploy
    _log("Auto-repair: n8n unreachable — triggering Railway redeploy")
    result = railway_redeploy.invoke({})
    if "error" in result.lower():
        return None
    _log(f"Railway redeploy triggered: {result[:100]}")
    time.sleep(30)  # wait for service to come back up
    return f"Triggered Railway redeploy to restart n8n: {result[:100]}"


def _fix_reactivate_workflows(error: str, context: dict) -> str | None:
    """Workflows went inactive after a redeploy — reactivate all that should be active."""
    from ..tools.n8n_tools import n8n_list_workflows, n8n_activate_workflow
    workflows_raw = n8n_list_workflows.invoke({})
    if "error" in workflows_raw.lower() or not workflows_raw.strip():
        return None
    reactivated = []
    for line in workflows_raw.splitlines():
        if line.startswith("INACTIVE") and "|" in line:
            parts = line.split("|")
            if len(parts) >= 2:
                wf_id = parts[1].strip()
                wf_name = parts[2].strip() if len(parts) > 2 else wf_id
                # Reactivate workflows that were previously marked active
                # (heuristic: any workflow whose name doesn't contain "disabled" or "test")
                if not any(kw in wf_name.lower() for kw in ("disabled", "test", "draft", "dev")):
                    result = n8n_activate_workflow.invoke({"workflow_id": wf_id})
                    reactivated.append(f"{wf_name} ({wf_id})")
                    _log(f"Auto-reactivated workflow: {wf_name} ({wf_id})")
    if reactivated:
        return f"Reactivated {len(reactivated)} workflows: {', '.join(reactivated)}"
    return None


def _fix_check_api_key(error: str, context: dict) -> str | None:
    """API key is wrong or expired — check Railway variables."""
    from ..tools.railway_tools import railway_list_variables
    vars_raw = railway_list_variables.invoke({})
    if "N8N_API_KEY" in vars_raw:
        _log("N8N_API_KEY is set in Railway but requests still fail — key may be invalid")
        return "N8N_API_KEY is set in Railway. Key may be expired — regenerate it in n8n Settings → API → Create API Key, then update Railway variable."
    else:
        _log("N8N_API_KEY not found in Railway variables")
        return "N8N_API_KEY is NOT set in Railway variables. Add it: n8n Settings → API → Create API Key → Railway Variables → N8N_API_KEY=<key>"


def _fix_check_base_url(error: str, context: dict) -> str | None:
    """N8N_BASE_URL may be wrong or the service may have changed domain.

    Attempts auto-detection: reads Railway services to find the n8n service URL
    and compares it against the current N8N_BASE_URL variable. If they differ
    (e.g. after a Railway domain change), updates the variable automatically.
    """
    import re as _re
    from ..tools.railway_tools import railway_list_services, railway_list_variables

    vars_raw = railway_list_variables.invoke({})
    services_raw = railway_list_services.invoke({})
    _log(f"URL check — N8N_BASE_URL in vars: {'N8N_BASE_URL' in vars_raw}")

    # Extract the current N8N_BASE_URL value
    current_url = ""
    for line in vars_raw.splitlines():
        if "N8N_BASE_URL" in line and "=" in line:
            current_url = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

    # Find Railway service URLs and identify the n8n one
    service_urls = _re.findall(r'https://[\w.-]+\.up\.railway\.app', services_raw)
    n8n_service_url = None
    for url in service_urls:
        # Check if the line/context mentioning this URL contains "n8n"
        idx = services_raw.find(url)
        context_window = services_raw[max(0, idx - 100):idx + len(url) + 50].lower()
        if "n8n" in context_window:
            n8n_service_url = url
            break

    if n8n_service_url and current_url and n8n_service_url != current_url:
        # Domain mismatch detected — auto-fix
        _log(f"Domain mismatch: current={current_url} actual={n8n_service_url} — auto-fixing")
        try:
            from ..tools.railway_tools import railway_set_variable
            railway_set_variable.invoke({"key": "N8N_BASE_URL", "value": n8n_service_url})
            # Also update the runtime config so the fix takes effect immediately
            settings.n8n_base_url = n8n_service_url
            return (
                f"Auto-fixed N8N_BASE_URL domain mismatch: {current_url} → {n8n_service_url}. "
                f"Railway variable updated and runtime config refreshed."
            )
        except Exception as e:
            _log(f"Auto-fix failed: {e}")
            return (
                f"Domain mismatch detected: current={current_url} actual={n8n_service_url}. "
                f"Auto-fix failed: {e}. Update N8N_BASE_URL manually in Railway."
            )

    return (
        f"N8N_BASE_URL config check.\n"
        f"Current URL: {current_url or '(not set)'}\n"
        f"Detected n8n service: {n8n_service_url or '(not found in services)'}\n"
        f"Services: {services_raw[:300]}\n"
        f"Verify the n8n service domain in Railway matches the N8N_BASE_URL variable."
    )


# ── Error pattern registry ────────────────────────────────────────────────────

_N8N_REPAIR_RULES: list[tuple[str, object]] = [
    ("application not found",       _fix_restart_n8n),
    ("connection refused",          _fix_restart_n8n),
    ("connect error",               _fix_restart_n8n),
    ("timeout",                     _fix_restart_n8n),
    ("network error",               _fix_restart_n8n),
    ("502",                         _fix_restart_n8n),
    ("503",                         _fix_restart_n8n),
    ("econnrefused",                _fix_restart_n8n),
    ("unauthorized",                _fix_check_api_key),
    ("401",                         _fix_check_api_key),
    ("403",                         _fix_check_api_key),
    ("invalid api key",             _fix_check_api_key),
    ("n8n_base_url not set",        _fix_check_base_url),
    ("not set",                     _fix_check_base_url),
    ("domain",                      _fix_check_base_url),
    ("name or service not known",   _fix_check_base_url),
    ("name resolution",             _fix_check_base_url),
    ("workflow.*inactive",          _fix_reactivate_workflows),
    ("workflows.*deactivated",      _fix_reactivate_workflows),
]


def attempt_n8n_repair(error: str, context: dict | None = None) -> tuple[bool, list[str]]:
    """
    Analyse an n8n error string, apply all matching fixes.
    Returns (any_fix_applied: bool, descriptions: list[str]).
    """
    import re
    lower = error.lower()
    applied: list[str] = []
    seen_fns: set = set()

    for pattern, fix_fn in _N8N_REPAIR_RULES:
        if re.search(pattern, lower) and fix_fn not in seen_fns:
            seen_fns.add(fix_fn)
            try:
                result = fix_fn(error, context or {})
                if result:
                    applied.append(result)
                    _log(f"Auto-repair applied [{fix_fn.__name__}]: {result[:120]}")
            except Exception as e:
                _log(f"Auto-repair warning [{fix_fn.__name__}]: {e}")

    return bool(applied), applied


# ── Health check ──────────────────────────────────────────────────────────────

def n8n_health_check() -> dict:
    """
    Full n8n health snapshot. Returns a dict with:
      reachable, active_workflows, inactive_workflows,
      recent_failures, recent_successes, issues (list of strings)
    """
    from ..tools.n8n_tools import n8n_list_workflows, n8n_list_executions

    result = {
        "reachable": False,
        "active_workflows": 0,
        "inactive_workflows": 0,
        "recent_failures": 0,
        "recent_successes": 0,
        "issues": [],
    }

    # 1. Reachability
    wf_raw = n8n_list_workflows.invoke({})
    if any(e in wf_raw.lower() for e in ("error", "refused", "timeout", "not found", "not set")):
        result["issues"].append(f"n8n unreachable: {wf_raw[:200]}")
        _log(f"Health check: UNREACHABLE — {wf_raw[:150]}")
        return result

    result["reachable"] = True

    # 2. Workflow counts
    for line in wf_raw.splitlines():
        if line.startswith("ACTIVE"):
            result["active_workflows"] += 1
        elif line.startswith("INACTIVE"):
            result["inactive_workflows"] += 1

    if result["inactive_workflows"] > result["active_workflows"] and result["active_workflows"] == 0:
        result["issues"].append(
            f"All {result['inactive_workflows']} workflows are INACTIVE — "
            "may have been deactivated by a redeploy"
        )

    # 3. Recent execution failures (only count last 24 hours)
    from datetime import datetime, timedelta, timezone
    _cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    exec_raw = n8n_list_executions.invoke({"workflow_id": "", "limit": 30})
    if isinstance(exec_raw, str) and not exec_raw.startswith("["):
        for line in exec_raw.splitlines():
            # Format: STATUS | exec:ID | name | startedAt → stoppedAt
            # Only count executions within the last 24 hours
            _parts = line.split("|")
            if len(_parts) >= 4:
                _time_part = _parts[3].strip()  # "startedAt → stoppedAt"
                _started = _time_part.split("→")[0].strip() if "→" in _time_part else ""
                if _started and _started < _cutoff:
                    continue  # stale execution — skip
            upper = line.upper()
            if "ERROR" in upper or "CRASHED" in upper or "FAILED" in upper:
                result["recent_failures"] += 1
            elif "SUCCESS" in upper:
                result["recent_successes"] += 1

    if result["recent_failures"] >= 3:
        result["issues"].append(
            f"{result['recent_failures']} recent execution failures detected"
        )

    _log(
        f"Health check: reachable={result['reachable']} "
        f"active={result['active_workflows']} inactive={result['inactive_workflows']} "
        f"failures={result['recent_failures']} issues={len(result['issues'])}"
    )
    return result


# ── Scheduler hook ────────────────────────────────────────────────────────────

def monitor_n8n() -> None:
    """
    Called by the scheduler every 15 minutes.
    Checks n8n health and auto-repairs any detected issues.
    """
    from ..activity_log import bg_log as _activity_log

    if not settings.n8n_base_url or not settings.n8n_api_key:
        return  # n8n not configured — skip silently

    health = n8n_health_check()

    if not health["issues"]:
        _activity_log(
            f"n8n healthy — active={health['active_workflows']} inactive={health['inactive_workflows']} "
            f"recent_failures={health['recent_failures']}",
            source="n8n_monitor",
        )
        return  # all good — no action needed

    # Issues found — attempt auto-repair for each
    for issue in health["issues"]:
        _activity_log(f"Issue detected: {issue[:200]}", source="n8n_monitor")
        fixed, fixes = attempt_n8n_repair(issue)
        if fixed:
            _log(f"Auto-repair resolved: {issue[:100]} → {fixes}")
            _activity_log(f"Auto-repair applied: {'; '.join(fixes)[:300]}", source="n8n_monitor")
        else:
            # Can't auto-fix — escalate to self_improve_agent for autonomous investigation
            _activity_log(f"Auto-repair had no fix for issue — escalating to self_improve_agent", source="n8n_monitor")
            try:
                from ..agents.self_improve_agent import run_self_improve_agent
                run_self_improve_agent(
                    f"n8n HEALTH ALERT — autonomous monitor detected an issue:\n"
                    f"{issue}\n\n"
                    f"Full health snapshot:\n{json.dumps(health, indent=2)}\n\n"
                    f"Investigate using railway_get_logs, railway_get_deployment_status, "
                    f"and n8n_list_workflows. Apply a fix if you can do so safely. "
                    f"Do not ask the user — act autonomously.",
                    authorized=False,
                )
            except Exception as e:
                _log(f"Escalation to self_improve_agent failed: {e}")
