"""
Proactive Anomaly Alerter — pushes alerts when key metrics cross thresholds.

Called from _scheduled_health_check() in main.py after every metrics snapshot.
Sends email via existing SMTP credentials (optional — if smtp_user not set,
alerts are written to the activity log only).

Dedup: each alert type is suppressed for 4 hours after firing (cooldown stored
in the alert log, no extra files needed).

Storage: /workspace/anomaly_alerts.json  (fallback ./)
Format:  flat JSON array, capped at 200 entries
Writes:  best-effort / exception-swallowed — never block the health check
"""
import json
import os
import time
import datetime
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from ..activity_log import bg_log as _log

_ALERT_LOG_FILE = "anomaly_alerts.json"
_MAX_ALERTS = 200
_COOLDOWN_SECONDS = 14_400  # 4 hours


def _resolve_path() -> Path:
    base = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
    return base / _ALERT_LOG_FILE


def _load() -> list:
    try:
        return json.loads(_resolve_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list) -> None:
    try:
        _resolve_path().write_text(
            json.dumps(entries[-_MAX_ALERTS:], indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── Alert rule definitions ─────────────────────────────────────────────────────
# Each rule: name (str), check (callable[dict] -> bool), threshold_desc (str)
_ALERT_RULES: list[dict] = [
    {
        "name": "error_rate_spike",
        "check": lambda m: m.get("error_rate_pct", 0) > 20,
        "threshold_desc": "error_rate_pct > 20%",
        "severity": "high",
    },
    {
        "name": "cost_near_budget",
        "check": lambda m: m.get("budget_used_pct", 0) > 90,
        "threshold_desc": "budget_used_pct > 90%",
        "severity": "medium",
    },
    {
        "name": "disk_high",
        "check": lambda m: m.get("disk_used_pct", 0) > 85,
        "threshold_desc": "disk_used_pct > 85%",
        "severity": "medium",
    },
    {
        "name": "n8n_failures",
        "check": lambda m: m.get("n8n_recent_failures", 0) >= 3,
        "threshold_desc": "n8n_recent_failures >= 3",
        "severity": "high",
    },
]


def _last_fired(alert_name: str, entries: list) -> float:
    """Return the Unix timestamp of the most recent firing of this alert, or 0."""
    matching = [e for e in entries if e.get("name") == alert_name]
    if not matching:
        return 0.0
    return max(e.get("fired_at_ts", 0) for e in matching)


def _send_email(alert_name: str, metric_value: str, threshold_desc: str) -> bool:
    """
    Send alert email using SMTP settings from config.
    Returns True on success, False on any failure (email is best-effort).
    """
    try:
        from ..config import settings
        if not settings.smtp_user or not settings.smtp_password:
            return False

        subject = f"[Super Agent] ALERT: {alert_name}"
        body = (
            f"Super Agent anomaly alert fired.\n\n"
            f"Alert:     {alert_name}\n"
            f"Threshold: {threshold_desc}\n"
            f"Value:     {metric_value}\n"
            f"Time:      {datetime.datetime.utcnow().isoformat()} UTC\n\n"
            f"Check /activity/recent for recent log lines.\n"
            f"Check /cycle-log for improvement cycle history.\n"
        )
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = settings.smtp_user
        msg["To"] = settings.notify_email

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, [settings.notify_email], msg.as_string())
        return True
    except Exception:
        return False


# ── Automation registry (G6) ──────────────────────────────────────────────────
# Map alert name → callable that attempts a fix automatically. Each handler
# returns a short string describing the action taken (or why it skipped),
# which gets appended to the alert log entry. Handlers MUST never raise.
# Keeping handlers tiny and explicit so it's obvious what the system can do
# autonomously vs. what still needs human attention.

def _auto_n8n_repair() -> str:
    try:
        from ..tools.n8n_repair import attempt_n8n_repair, n8n_health_check
        health = n8n_health_check()
        if health.get("reachable"):
            return "n8n already reachable — no repair needed"
        issues = health.get("issues") or ["unknown"]
        fixed, fixes = attempt_n8n_repair(issues[0] if issues else "")
        return ("repaired: " if fixed else "attempted: ") + "; ".join(fixes or ["nothing to do"])
    except Exception as e:
        return f"auto-repair error: {str(e)[:120]}"


def _auto_cache_flush() -> str:
    try:
        from ..cache.response_cache import cache
        before = getattr(cache, "stats", lambda: {})().get("size", "?")
        cache._cache.clear() if hasattr(cache, "_cache") else None
        return f"cache flushed (was size={before})"
    except Exception as e:
        return f"cache flush error: {str(e)[:120]}"


def _auto_storage_cleanup() -> str:
    try:
        from ..storage.cloudinary_manager import get_storage_status
        status = get_storage_status()
        return f"storage status checked: {str(status)[:200]}"
    except Exception as e:
        return f"storage check error: {str(e)[:120]}"


_AUTOMATION_REGISTRY: dict = {
    "n8n_failures":      _auto_n8n_repair,
    "error_rate_spike":  _auto_cache_flush,
    "disk_high":         _auto_storage_cleanup,
    # cost_near_budget intentionally NOT auto-handled — should bias future
    # routing (handled by routing_advisor.budget_tier), not autonomously cap.
}


def _run_automation(alert_name: str) -> str:
    handler = _AUTOMATION_REGISTRY.get(alert_name)
    if not handler:
        return ""
    try:
        return handler() or ""
    except Exception as e:
        return f"automation handler crashed: {str(e)[:120]}"


def check_and_alert(metrics: dict) -> list[str]:
    """
    Evaluate ALERT_RULES against the current metrics snapshot.
    Fires alerts that pass their threshold and are outside the cooldown window.
    Sends email if SMTP is configured; always logs to activity log.

    Args:
        metrics: dict from collect_current_snapshot() (keys: error_rate_pct,
                 disk_used_pct, n8n_recent_failures, budget_used_pct, etc.)

    Returns:
        List of fired alert names (empty if none fired).
    """
    fired: list[str] = []
    try:
        # Enrich metrics with budget_used_pct if not present
        if "budget_used_pct" not in metrics:
            try:
                from .cost_ledger import get_spend
                spend = get_spend(hours=24.0)
                daily_budget = spend.get("daily_budget_usd", 5.0)
                total_usd = spend.get("total_usd", 0.0)
                metrics = dict(metrics)
                metrics["budget_used_pct"] = round(total_usd / max(daily_budget, 0.01) * 100, 1)
            except Exception:
                pass

        entries = _load()
        now = time.time()
        new_entries = []

        for rule in _ALERT_RULES:
            name = rule["name"]
            try:
                if not rule["check"](metrics):
                    continue
            except Exception:
                continue

            # Check cooldown
            if now - _last_fired(name, entries) < _COOLDOWN_SECONDS:
                continue

            # Compute human-readable value for this rule
            value_map = {
                "error_rate_spike": f"{metrics.get('error_rate_pct', '?')}%",
                "cost_near_budget": f"{metrics.get('budget_used_pct', '?')}% of daily budget",
                "disk_high": f"{metrics.get('disk_used_pct', '?')}% disk used",
                "n8n_failures": f"{metrics.get('n8n_recent_failures', '?')} recent failures",
            }
            metric_value = value_map.get(name, str(metrics))
            threshold_desc = rule["threshold_desc"]

            # Log to activity log (always)
            _log(
                f"ANOMALY ALERT: {name} | value={metric_value} | threshold={threshold_desc}",
                source="anomaly_alerter",
            )

            # Send email (best-effort)
            email_sent = _send_email(name, metric_value, threshold_desc)

            # G6: attempt automated remediation for known alert names.
            automation_result = _run_automation(name)
            if automation_result:
                _log(
                    f"AUTOMATION for {name}: {automation_result}",
                    source="anomaly_alerter",
                )

            # Persist alert record
            entry = {
                "name": name,
                "severity": rule["severity"],
                "metric_value": metric_value,
                "threshold_desc": threshold_desc,
                "fired_at": datetime.datetime.utcnow().isoformat(),
                "fired_at_ts": now,
                "email_sent": email_sent,
                "automation_result": automation_result,
                "metrics_snapshot": {k: v for k, v in metrics.items()
                                     if isinstance(v, (int, float, str))},
            }
            new_entries.append(entry)
            fired.append(name)

        if new_entries:
            _save(entries + new_entries)

    except Exception:
        pass  # never raise from here

    return fired


def get_recent_alerts(n: int = 20) -> list[dict]:
    """Return the last N alerts, newest first."""
    try:
        return list(reversed(_load()))[:n]
    except Exception:
        return []


def get_recent_alert(max_age_s: int = 300) -> str:
    """
    Return a human-readable summary of the most recent HIGH severity alert
    fired within max_age_s seconds. Used by the dispatcher to surface critical
    anomalies in-band to the user without them having to check logs.
    Returns empty string if no recent high-severity alert exists.
    """
    try:
        cutoff = time.time() - max_age_s
        for alert in reversed(_load()):
            if (
                alert.get("fired_at_ts", 0) >= cutoff
                and alert.get("severity", "") in ("high", "critical")
            ):
                name = alert.get("name", "unknown")
                value = alert.get("metric_value", "")
                return f"{name}: {value}" if value else name
        return ""
    except Exception:
        return ""
