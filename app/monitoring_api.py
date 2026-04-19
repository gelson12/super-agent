"""
Monitoring API — FastAPI router mounted at /monitoring.

Endpoints:
  GET  /monitoring/              — serve the futuristic dashboard HTML
  GET  /monitoring/api/status    — latest snapshot summary
  GET  /monitoring/api/reports   — paginated snapshot history
  GET  /monitoring/api/suggestions — pending / all suggestions
  POST /monitoring/api/suggestions/{id}/approve
  POST /monitoring/api/reject/{id}
  POST /monitoring/api/run-check — trigger an immediate health check (async)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from .config import settings

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
_MONITOR_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "system_health_monitor.py")


# ── DB helper ─────────────────────────────────────────────────────────────────

def _db():
    import psycopg2
    import psycopg2.extras
    url = (os.environ.get("DATABASE_URL") or "").replace("postgres://", "postgresql://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _ensure_tables() -> None:
    """Idempotent — called on first request if tables don't exist."""
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monitoring_snapshots (
                    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    run_type        VARCHAR(30) NOT NULL DEFAULT 'scheduled',
                    overall_status  VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    n8n_status      VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    cli_status      VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    agent_status    VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    checks_passed   INT         NOT NULL DEFAULT 0,
                    checks_failed   INT         NOT NULL DEFAULT 0,
                    checks_total    INT         NOT NULL DEFAULT 0,
                    data            JSONB       NOT NULL DEFAULT '{}',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS monitoring_snapshots_created_idx
                    ON monitoring_snapshots(created_at DESC);

                CREATE TABLE IF NOT EXISTS monitoring_suggestions (
                    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    snapshot_id     UUID        REFERENCES monitoring_snapshots(id) ON DELETE SET NULL,
                    target_system   VARCHAR(100) NOT NULL,
                    severity        VARCHAR(20)  NOT NULL DEFAULT 'medium',
                    title           VARCHAR(400) NOT NULL,
                    description     TEXT         NOT NULL,
                    proposed_fix    TEXT,
                    status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
                    reviewed_by     VARCHAR(100),
                    reviewed_at     TIMESTAMPTZ,
                    applied_at      TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS monitoring_suggestions_status_idx
                    ON monitoring_suggestions(status);
                CREATE INDEX IF NOT EXISTS monitoring_suggestions_created_idx
                    ON monitoring_suggestions(created_at DESC);
            """)
        conn.commit()
        conn.close()
    except Exception:
        pass  # Fail silently — endpoints will return their own errors


_tables_ensured = False


def _get_db():
    global _tables_ensured
    if not _tables_ensured:
        _ensure_tables()
        _tables_ensured = True
    return _db()


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
def monitoring_dashboard():
    path = os.path.join(_STATIC_DIR, "monitoring.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return JSONResponse({"error": "monitoring.html not found"}, status_code=404)


# ── API: latest status ────────────────────────────────────────────────────────

@router.get("/api/status")
def api_status():
    try:
        conn = _get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, run_type, overall_status, n8n_status, cli_status, agent_status,
                       checks_passed, checks_failed, checks_total, created_at
                FROM monitoring_snapshots
                ORDER BY created_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()
        conn.close()
        if not row:
            return {"status": "no_data", "message": "No health check has run yet."}
        return dict(row)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: reports history ──────────────────────────────────────────────────────

@router.get("/api/reports")
def api_reports(limit: int = Query(20, ge=1, le=100)):
    try:
        conn = _get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, run_type, overall_status, n8n_status, cli_status, agent_status,
                       checks_passed, checks_failed, checks_total, created_at
                FROM monitoring_snapshots
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: report detail ────────────────────────────────────────────────────────

@router.get("/api/reports/{report_id}")
def api_report_detail(report_id: str):
    try:
        conn = _get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM monitoring_snapshots WHERE id = %s",
                (report_id,)
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Report not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: suggestions ──────────────────────────────────────────────────────────

@router.get("/api/suggestions")
def api_suggestions(
    status: str = Query("pending", description="pending | approved | rejected | all"),
    limit: int = Query(50, ge=1, le=200),
):
    try:
        conn = _get_db()
        with conn.cursor() as cur:
            if status == "all":
                cur.execute("""
                    SELECT * FROM monitoring_suggestions
                    ORDER BY
                        CASE severity
                            WHEN 'critical' THEN 1
                            WHEN 'high'     THEN 2
                            WHEN 'medium'   THEN 3
                            ELSE 4
                        END,
                        created_at DESC
                    LIMIT %s
                """, (limit,))
            else:
                cur.execute("""
                    SELECT * FROM monitoring_suggestions
                    WHERE status = %s
                    ORDER BY
                        CASE severity
                            WHEN 'critical' THEN 1
                            WHEN 'high'     THEN 2
                            WHEN 'medium'   THEN 3
                            ELSE 4
                        END,
                        created_at DESC
                    LIMIT %s
                """, (status, limit))
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: approve suggestion ───────────────────────────────────────────────────

@router.post("/api/suggestions/{suggestion_id}/approve")
def api_approve(suggestion_id: str):
    try:
        conn = _get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM monitoring_suggestions WHERE id = %s",
                (suggestion_id,)
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                raise HTTPException(status_code=404, detail="Suggestion not found")
            if row["status"] != "pending":
                conn.close()
                return {"ok": False, "message": f"Already {row['status']}"}

            cur.execute("""
                UPDATE monitoring_suggestions
                SET status = 'approved', reviewed_by = 'human', reviewed_at = NOW()
                WHERE id = %s
            """, (suggestion_id,))
        conn.commit()

        # Forward to self-improvement agent asynchronously
        suggestion = dict(row)
        _forward_to_self_improve(suggestion, action="approve")
        conn.close()

        return {"ok": True, "message": "Approved. Self-improvement agent has been notified."}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: reject suggestion ────────────────────────────────────────────────────

@router.post("/api/suggestions/{suggestion_id}/reject")
def api_reject(suggestion_id: str):
    try:
        conn = _get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM monitoring_suggestions WHERE id = %s",
                (suggestion_id,)
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                raise HTTPException(status_code=404, detail="Suggestion not found")
            if row["status"] != "pending":
                conn.close()
                return {"ok": False, "message": f"Already {row['status']}"}
            cur.execute("""
                UPDATE monitoring_suggestions
                SET status = 'rejected', reviewed_by = 'human', reviewed_at = NOW()
                WHERE id = %s
            """, (suggestion_id,))
        conn.commit()
        conn.close()
        return {"ok": True, "message": "Rejected."}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: manual health check trigger ─────────────────────────────────────────

_check_running = False


@router.post("/api/run-check")
def api_run_check():
    global _check_running
    if _check_running:
        return {"ok": False, "message": "A health check is already running. Check back in 60s."}

    def _run():
        global _check_running
        _check_running = True
        try:
            env = {**os.environ, "RUN_TYPE": "manual"}
            subprocess.run(
                [sys.executable, _MONITOR_SCRIPT],
                env=env,
                timeout=300,
                capture_output=True,
            )
        except Exception:
            pass
        finally:
            _check_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Health check started. Results will appear in the dashboard within ~60s."}


# ── API: check running status ─────────────────────────────────────────────────

@router.get("/api/run-check/status")
def api_check_status():
    return {"running": _check_running}


# ── Self-Improvement forwarding ───────────────────────────────────────────────

def _forward_to_self_improve(suggestion: dict, action: str) -> None:
    """
    Fire-and-forget: POST an approved suggestion to super-agent /chat
    so the self-improvement agent can act on it.
    Must not block the HTTP response.
    """
    def _send():
        try:
            import httpx
            agent_url = (os.environ.get("SUPER_AGENT_URL") or "http://localhost:8000").rstrip("/")
            ui_password = settings.ui_password or ""

            prompt = (
                f"MONITORING SYSTEM — APPROVED IMPROVEMENT SUGGESTION\n\n"
                f"A human operator has approved the following finding for you to investigate and fix:\n\n"
                f"System: {suggestion.get('target_system', '?')}\n"
                f"Severity: {suggestion.get('severity', '?').upper()}\n"
                f"Title: {suggestion.get('title', '?')}\n\n"
                f"Description:\n{suggestion.get('description', '')}\n\n"
                f"Proposed fix:\n{suggestion.get('proposed_fix', 'None provided — use your judgement.')}\n\n"
                f"Please investigate this issue, confirm whether it is still present, "
                f"and apply the fix if appropriate. Use the alpha0 safe word if you need "
                f"to make changes to n8n workflows or GitHub. Report back what you find and what you did."
            )

            headers = {"Content-Type": "application/json"}
            if ui_password:
                headers["X-Token"] = ui_password

            httpx.post(
                f"{agent_url}/chat",
                json={"message": prompt, "session_id": f"monitoring-{suggestion['id'][:8]}"},
                headers=headers,
                timeout=30,
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()
