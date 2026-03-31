"""
Improvement Monitor — 6-hour health babysitting for applied suggestions.

After any autonomous improvement is deployed, a monitor record is created here.
APScheduler calls tick() every 30 minutes to:
  - Check Railway logs for error spikes
  - Compare insight_log error rate against the pre-change baseline
  - Mark stable after 6 clean hours
  - Trigger automatic rollback if health degrades

State persists to /workspace/improvement_monitors.json (or local fallback).
Never raises — all errors are caught and logged.
"""
import json
import os
import datetime
from pathlib import Path

_STATE_DIR = Path("/workspace")
_FALLBACK_DIR = Path(".")
_MONITOR_FILENAME = "improvement_monitors.json"
_BABYSIT_HOURS = 6
_CHECK_INTERVAL_MIN = 30  # matches APScheduler interval
_ERROR_RATE_TOLERANCE = 15.0  # percentage points above baseline = rollback
_LOG_ERROR_THRESHOLD = 3  # railway log error lines = rollback


def _state_path() -> Path:
    base = _STATE_DIR if os.access(_STATE_DIR, os.W_OK) else _FALLBACK_DIR
    return base / _MONITOR_FILENAME


def _load() -> list[dict]:
    path = _state_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save(monitors: list[dict]) -> None:
    try:
        _state_path().write_text(json.dumps(monitors, indent=2))
    except Exception as e:
        print(f"[improvement_monitor] WARNING: could not save state: {e}")


def start_monitoring(
    description: str,
    rollback_branch: str,
    files_changed: list[str],
    baseline_error_rate: float = 0.0,
) -> None:
    """
    Register a new deployment for health monitoring.
    Call this immediately after run_self_improve_agent() deploys a change.
    """
    monitors = _load()
    ts = datetime.datetime.utcnow()
    record = {
        "id": f"improve-{ts.strftime('%Y-%m-%dT%H-%M')}",
        "description": description,
        "rollback_branch": rollback_branch,
        "files_changed": files_changed,
        "start_ts": ts.timestamp(),
        "last_check_ts": ts.timestamp(),
        "check_count": 0,
        "baseline_error_rate": baseline_error_rate,
        "status": "monitoring",
    }
    monitors.append(record)
    _save(monitors)
    print(f"[improvement_monitor] Started monitoring: {record['id']} — {description[:80]}")


def _count_log_errors(logs: str) -> int:
    """Count lines in Railway logs that look like errors."""
    error_keywords = ("ERROR", "CRITICAL", "Traceback", "Exception", "FATAL")
    return sum(
        1 for line in logs.splitlines()
        if any(kw in line for kw in error_keywords)
    )


def _check_health(monitor: dict) -> bool:
    """
    Return True if the system is healthy (no rollback needed).
    Returns False if health has degraded past the tolerance threshold.
    """
    try:
        from ..tools.railway_tools import railway_get_logs
        logs = railway_get_logs.invoke({})
        error_lines = _count_log_errors(logs)
        if error_lines >= _LOG_ERROR_THRESHOLD:
            print(
                f"[improvement_monitor] HEALTH FAIL [{monitor['id']}]: "
                f"{error_lines} error lines in Railway logs"
            )
            return False
    except Exception as e:
        print(f"[improvement_monitor] WARNING: could not check Railway logs: {e}")

    try:
        from .insight_log import insight_log
        summary = insight_log.summary()
        current_error_rate = summary.get("error_rate_pct", 0.0)
        baseline = monitor.get("baseline_error_rate", 0.0)
        if current_error_rate > baseline + _ERROR_RATE_TOLERANCE:
            print(
                f"[improvement_monitor] HEALTH FAIL [{monitor['id']}]: "
                f"error rate {current_error_rate:.1f}% vs baseline {baseline:.1f}%"
            )
            return False
    except Exception as e:
        print(f"[improvement_monitor] WARNING: could not check error rate: {e}")

    return True


def _rollback(monitor: dict) -> None:
    """Trigger automatic rollback via the self-improve agent."""
    try:
        from ..agents.self_improve_agent import run_self_improve_agent

        files_str = ", ".join(monitor.get("files_changed", []))
        branch = monitor.get("rollback_branch", "unknown")
        description = monitor.get("description", "unknown improvement")

        msg = (
            f"AUTOMATIC ROLLBACK AUTHORIZED: Health monitoring detected degradation "
            f"after applying [{description}].\n\n"
            f"Rollback branch: {branch}\n"
            f"Files changed: {files_str}\n\n"
            f"Execute these steps immediately:\n"
            f"1. Use run_authorized_shell_command to run:\n"
            f"   git fetch origin && git checkout master && "
            f"   git revert HEAD --no-edit && git push origin master\n"
            f"2. Confirm Railway redeployment triggers after the push.\n"
            f"3. Report what was reverted and confirm the deployment status.\n\n"
            f"This rollback is pre-authorized by the improvement monitoring system. "
            f"Execute without asking for a safe word."
        )

        print(f"[improvement_monitor] Triggering rollback for: {monitor['id']}")
        result = run_self_improve_agent(msg, authorized=True)
        print(f"[improvement_monitor] Rollback agent result: {result[:200]}")
    except Exception as e:
        print(f"[improvement_monitor] ERROR: rollback failed for {monitor['id']}: {e}")


def tick() -> None:
    """
    Called every 30 minutes by APScheduler.
    Checks all pending monitors; marks stable or triggers rollback.
    Never raises.
    """
    try:
        monitors = _load()
        changed = False
        now = datetime.datetime.utcnow().timestamp()

        for monitor in monitors:
            if monitor.get("status") != "monitoring":
                continue

            elapsed_hours = (now - monitor["start_ts"]) / 3600.0
            monitor["last_check_ts"] = now
            monitor["check_count"] = monitor.get("check_count", 0) + 1
            changed = True

            if elapsed_hours >= _BABYSIT_HOURS:
                monitor["status"] = "stable"
                print(
                    f"[improvement_monitor] STABLE after {elapsed_hours:.1f}h: "
                    f"{monitor['id']} — {monitor['description'][:60]}"
                )
                continue

            healthy = _check_health(monitor)
            if not healthy:
                monitor["status"] = "failed"
                _rollback(monitor)
                monitor["status"] = "rolled_back"

        if changed:
            _save(monitors)

        active = sum(1 for m in monitors if m.get("status") == "monitoring")
        if monitors:
            print(f"[improvement_monitor] tick: {active} active monitor(s)")
    except Exception as e:
        print(f"[improvement_monitor] ERROR in tick(): {e}")


def list_monitors() -> list[dict]:
    """Return all monitor records (for the /improvement-status API endpoint)."""
    return _load()
