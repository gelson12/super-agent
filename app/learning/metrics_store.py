"""
Metrics store — time-series snapshots for predictive failure detection.
Written every 30-min health check. Slope analysis catches drift before incidents.
API: GET /metrics/trends, GET /metrics/history
"""
import json, os, time
from pathlib import Path
from typing import Any

_DIR = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
METRICS_PATH = _DIR / "agent_metrics.json"
_MAX = 2000


def record_snapshot(data: dict) -> None:
    snap = {"ts": round(time.time(), 1), **data}
    try:
        existing = _load()
        existing.append(snap)
        if len(existing) > _MAX:
            existing = existing[-_MAX:]
        METRICS_PATH.write_text(json.dumps(existing), encoding="utf-8")
    except Exception:
        pass


def _load() -> list:
    try:
        if METRICS_PATH.exists():
            return json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def get_recent(hours: float = 24.0) -> list:
    cutoff = time.time() - hours * 3600
    return [s for s in _load() if s.get("ts", 0) >= cutoff]


def _slope(values: list) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    xm, ym = sum(xs) / n, sum(values) / n
    num = sum((xs[i] - xm) * (values[i] - ym) for i in range(n))
    den = sum((xs[i] - xm) ** 2 for i in range(n))
    return round(num / den, 6) if den else 0.0


def get_trends(hours: float = 24.0) -> dict:
    snaps = get_recent(hours)
    if not snaps:
        return {"window_hours": hours, "samples": 0, "metrics": {}, "alerts": []}
    all_keys = set(k for s in snaps for k in s if k != "ts")
    metrics, alerts = {}, []
    for key in sorted(all_keys):
        vals = [s[key] for s in snaps if key in s and isinstance(s[key], (int, float))]
        if not vals:
            continue
        sl = _slope(vals)
        cur = vals[-1]
        alert = False
        if key == "error_rate_pct" and sl > 0.5:
            alert = True
            alerts.append(f"error_rate_pct rising +{sl:.2f}/interval (now {cur:.1f}%)")
        elif key == "disk_used_pct" and sl > 0.3 and cur > 70:
            alert = True
            alerts.append(f"disk filling fast +{sl:.2f}/interval (now {cur:.1f}%)")
        elif key == "n8n_recent_failures" and cur >= 3:
            alert = True
            alerts.append(f"n8n_recent_failures={cur}")
        elif key == "avg_resp_len" and sl < -50:
            alert = True
            alerts.append(f"avg_resp_len shrinking (truncation?) slope={sl:.1f}")
        metrics[key] = {"current": cur, "min": min(vals), "max": max(vals),
                        "slope": sl, "samples": len(vals), "alert": alert}
    return {"window_hours": hours, "samples": len(snaps),
            "first_ts": snaps[0]["ts"], "last_ts": snaps[-1]["ts"],
            "metrics": metrics, "alerts": alerts, "alert_count": len(alerts)}


def collect_current_snapshot() -> dict:
    """Gather live metrics. Called by scheduler every 30 min."""
    snap: dict[str, Any] = {}
    try:
        from ..learning.insight_log import insight_log as _il
        s = _il.summary()
        snap["error_rate_pct"] = float(s.get("error_rate_pct", 0.0))
        snap["total_interactions"] = int(s.get("total_interactions", 0))
        recent = _il._load_all()[-50:]
        if recent:
            snap["avg_resp_len"] = round(sum(e.get("resp_len", 0) for e in recent) / len(recent), 1)
    except Exception:
        pass
    try:
        import shutil
        u = shutil.disk_usage("/workspace")
        snap["disk_used_pct"] = round(u.used / u.total * 100, 1)
        snap["disk_free_gb"] = round(u.free / 1e9, 2)
    except Exception:
        pass
    try:
        from ..tools.n8n_repair import n8n_health_check
        from ..config import settings
        if settings.n8n_base_url and settings.n8n_api_key:
            h = n8n_health_check()
            snap["n8n_active"] = h.get("active_workflows", 0)
            snap["n8n_recent_failures"] = h.get("recent_failures", 0)
            snap["n8n_reachable"] = int(h.get("reachable", False))
    except Exception:
        pass
    try:
        from ..tools.flutter_tools import BUILD_PROGRESS_LOG
        if BUILD_PROGRESS_LOG.exists():
            lines = BUILD_PROGRESS_LOG.read_text(encoding="utf-8").strip().splitlines()
            last = lines[-1] if lines else ""
            snap["active_build"] = int(bool(lines) and "complete" not in last.lower() and "FAILED" not in last)
    except Exception:
        pass
    return snap
