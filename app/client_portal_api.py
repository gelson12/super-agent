"""
Client Status Portal — FastAPI router mounted at /client.

Endpoints:
  GET  /client/{token}           — public-facing status page (HTML)
  GET  /client/api/{token}       — JSON status for the token
  POST /client/api/create        — create a new client token (authenticated)
  GET  /client/api/list          — list all active tokens (authenticated)
  POST /client/api/{token}/update — update project status (authenticated)

Each token represents one client project. The page shows:
  - Project name, client name
  - Status (e.g. "In Progress", "Review", "Complete")
  - Progress steps with checkmarks
  - Next action / ETA message
  - Payment link button (if payment_link set)

Authentication for write endpoints: X-Token header matching N8N_API_KEY or UI_PASSWORD.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/client", tags=["client-portal"])

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db():
    import psycopg2
    import psycopg2.extras
    url = (os.environ.get("DATABASE_URL") or "").replace("postgres://", "postgresql://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


_tables_ready = False

def _ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bridge.client_projects (
                    token           VARCHAR(64)  PRIMARY KEY,
                    client_name     VARCHAR(200) NOT NULL,
                    project_name    VARCHAR(300) NOT NULL,
                    status          VARCHAR(50)  NOT NULL DEFAULT 'In Progress',
                    progress_steps  JSONB        NOT NULL DEFAULT '[]',
                    next_action     TEXT         NOT NULL DEFAULT '',
                    eta_text        VARCHAR(200) NOT NULL DEFAULT '',
                    payment_link    VARCHAR(500) NOT NULL DEFAULT '',
                    notes           TEXT         NOT NULL DEFAULT '',
                    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS bridge_client_projects_updated
                    ON bridge.client_projects(updated_at DESC);
            """)
        conn.commit()
        conn.close()
        _tables_ready = True
    except Exception:
        pass


def _auth(request: Request) -> bool:
    """Return True if the request carries a valid admin token."""
    token = request.headers.get("X-Token", "")
    n8n_key   = os.environ.get("N8N_API_KEY", "")
    ui_pass   = os.environ.get("UI_PASSWORD", "")
    for valid in [n8n_key, ui_pass]:
        if valid and secrets.compare_digest(token, valid):
            return True
    return False


# ── Pydantic models ───────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    client_name:    str  = Field(..., max_length=200)
    project_name:   str  = Field(..., max_length=300)
    status:         str  = Field(default="In Progress", max_length=50)
    progress_steps: list = Field(default_factory=list)
    next_action:    str  = Field(default="", max_length=2000)
    eta_text:       str  = Field(default="", max_length=200)
    payment_link:   str  = Field(default="", max_length=500)
    notes:          str  = Field(default="", max_length=5000)

class UpdateProjectRequest(BaseModel):
    status:         str | None = None
    progress_steps: list | None = None
    next_action:    str | None = None
    eta_text:       str | None = None
    payment_link:   str | None = None
    notes:          str | None = None


# ── CSS / HTML helpers ────────────────────────────────────────────────────────

_STATUS_COLOURS = {
    "In Progress": "#3b82f6",
    "Review":      "#f59e0b",
    "Complete":    "#10b981",
    "On Hold":     "#ef4444",
    "Delivered":   "#10b981",
}

def _render_portal(row: dict) -> str:
    status = row["status"]
    colour = _STATUS_COLOURS.get(status, "#6b7280")
    steps_html = ""
    for step in (row.get("progress_steps") or []):
        if isinstance(step, dict):
            done  = step.get("done", False)
            label = step.get("label", "")
        else:
            done, label = False, str(step)
        icon  = "✅" if done else "⏳"
        style = "color:#10b981;font-weight:600" if done else "color:#6b7280"
        steps_html += f'<li style="{style}">{icon} {label}</li>'

    payment_btn = ""
    if row.get("payment_link"):
        payment_btn = f"""
        <a href="{row['payment_link']}" target="_blank" style="
          display:inline-block;margin-top:1.5rem;padding:.75rem 2rem;
          background:#0a2540;color:#fff;border-radius:6px;text-decoration:none;
          font-weight:600;font-size:1rem">Pay Online →</a>"""

    next_action_html = (
        f"<div style='margin-top:1rem;padding:1rem;background:#f0f9ff;border-left:4px solid {colour};"
        f"border-radius:4px'><strong>Next:</strong> {row['next_action']}</div>"
        if row.get("next_action") else ""
    )
    eta_html = (
        f"<p style='color:#555;font-size:.9rem'>Estimated: {row['eta_text']}</p>"
        if row.get("eta_text") else ""
    )
    updated = row.get("updated_at")
    if hasattr(updated, "strftime"):
        updated_str = updated.strftime("%d %b %Y %H:%M UTC")
    else:
        updated_str = str(updated or "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{row['project_name']} — Project Status</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#f8fafc;color:#1a1a1a;padding:2rem 1rem}}
.card{{max-width:680px;margin:0 auto;background:#fff;border-radius:12px;
  box-shadow:0 4px 24px rgba(0,0,0,.08);overflow:hidden}}
.header{{background:#0a2540;color:#fff;padding:2rem}}
.header h1{{font-size:1.6rem;margin-bottom:.3rem}}
.header .sub{{opacity:.75;font-size:.9rem}}
.badge{{display:inline-block;padding:.3rem .8rem;border-radius:20px;
  font-size:.8rem;font-weight:700;color:#fff;margin-top:.75rem;
  background:{colour}}}
.body{{padding:2rem}}
.steps{{list-style:none;padding:0}}
.steps li{{padding:.5rem 0;font-size:1rem;border-bottom:1px solid #f0f0f0}}
.steps li:last-child{{border:none}}
.footer-note{{margin-top:2rem;font-size:.75rem;color:#aaa;text-align:center}}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="sub">Status update for {row['client_name']}</div>
    <h1>{row['project_name']}</h1>
    <div class="badge">{status}</div>
  </div>
  <div class="body">
    {'<ul class="steps">' + steps_html + '</ul>' if steps_html else ''}
    {next_action_html}
    {eta_html}
    {payment_btn}
    <div class="footer-note">Last updated: {updated_str}</div>
  </div>
</div>
</body>
</html>"""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/{token}", response_class=HTMLResponse, include_in_schema=False)
def client_portal_page(token: str):
    """Public-facing client status page."""
    _ensure_tables()
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bridge.client_projects WHERE token = %s", (token,))
            row = cur.fetchone()
        conn.close()
    except Exception as e:
        return HTMLResponse(f"<h2>Service temporarily unavailable.</h2>", status_code=503)

    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return HTMLResponse(_render_portal(dict(row)))


@router.get("/api/{token}")
def client_api_status(token: str):
    """JSON status for a client token."""
    _ensure_tables()
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bridge.client_projects WHERE token = %s", (token,))
            row = cur.fetchone()
        conn.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    data = dict(row)
    if "payment_link" in data:
        del data["payment_link"]  # don't leak payment URL in public JSON
    return data


@router.post("/api/create")
def client_api_create(req: CreateProjectRequest, request: Request):
    """Create a new client project and return its access token."""
    if not _auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    _ensure_tables()
    token = secrets.token_urlsafe(16)
    import json
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bridge.client_projects
                    (token, client_name, project_name, status, progress_steps,
                     next_action, eta_text, payment_link, notes)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            """, (
                token, req.client_name, req.project_name, req.status,
                json.dumps(req.progress_steps), req.next_action,
                req.eta_text, req.payment_link, req.notes,
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    base = os.environ.get("SUPER_AGENT_URL", "https://super-agent-production.up.railway.app").rstrip("/")
    return {
        "ok": True,
        "token": token,
        "url": f"{base}/client/{token}",
        "message": f"Client portal created. Share this URL: {base}/client/{token}",
    }


@router.post("/api/{token}/update")
def client_api_update(token: str, req: UpdateProjectRequest, request: Request):
    """Update a project's status fields."""
    if not _auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    _ensure_tables()
    import json

    fields: list[str] = []
    values: list[Any] = []
    if req.status         is not None: fields.append("status = %s");          values.append(req.status)
    if req.progress_steps is not None: fields.append("progress_steps = %s::jsonb"); values.append(json.dumps(req.progress_steps))
    if req.next_action    is not None: fields.append("next_action = %s");     values.append(req.next_action)
    if req.eta_text       is not None: fields.append("eta_text = %s");        values.append(req.eta_text)
    if req.payment_link   is not None: fields.append("payment_link = %s");    values.append(req.payment_link)
    if req.notes          is not None: fields.append("notes = %s");           values.append(req.notes)

    if not fields:
        return {"ok": False, "message": "No fields to update"}

    fields.append("updated_at = NOW()")
    values.append(token)

    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE bridge.client_projects SET {', '.join(fields)} WHERE token = %s",
                values,
            )
            if cur.rowcount == 0:
                conn.close()
                raise HTTPException(status_code=404, detail="Token not found")
        conn.commit()
        conn.close()
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {"ok": True, "message": "Project updated"}


@router.get("/api/list")
def client_api_list(request: Request):
    """List all client projects (admin only)."""
    if not _auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    _ensure_tables()
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT token, client_name, project_name, status, updated_at
                FROM bridge.client_projects
                ORDER BY updated_at DESC
                LIMIT 100
            """)
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    base = os.environ.get("SUPER_AGENT_URL", "https://super-agent-production.up.railway.app").rstrip("/")
    return [
        {**dict(r), "url": f"{base}/client/{r['token']}"}
        for r in rows
    ]
