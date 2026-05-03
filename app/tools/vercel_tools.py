"""
Vercel deployment tools.

Deploys static HTML to Vercel and returns a live preview URL within seconds.
Works with any single-file HTML (v0.dev output, manually written, or templated).

Prerequisites
-------------
Set Railway env var VERCEL_API_KEY (Vercel dashboard → Settings → Tokens → Create).
Optionally set VERCEL_TEAM_ID if deploying under a Vercel team.

Flow
----
  v0_generate_website(brief) → html string
      ↓
  vercel_deploy_html(html, slug) → "https://bridge-{slug}-xxx.vercel.app"
      ↓
  Share URL with client immediately — no GitHub Pages 75s wait.

API reference
-------------
POST https://api.vercel.com/v13/deployments
  Authorization: Bearer <VERCEL_API_KEY>
  Content-Type: application/json
  Body: { "name": "<project>", "files": [{"file": "index.html", "data": "<b64>", "encoding": "base64"}],
          "projectSettings": {"framework": null}, "target": "preview" }
Response: { "url": "bridge-slug-xxx.vercel.app", "id": "dpl_...", "readyState": "READY" }
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from langchain_core.tools import tool

from ..config import settings


def _vercel_api(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    """Make a Vercel REST API call. Returns parsed JSON or raises on HTTP error."""
    api_key = settings.vercel_api_key or os.environ.get("VERCEL_API_KEY", "")
    if not api_key:
        raise ValueError("VERCEL_API_KEY not configured — set it as a Railway env var")

    url = f"https://api.vercel.com{path}"
    team_id = os.environ.get("VERCEL_TEAM_ID", "")
    if team_id:
        url += ("&" if "?" in url else "?") + f"teamId={team_id}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _wait_for_ready(deployment_id: str, timeout_s: int = 120) -> str:
    """Poll until deployment reaches READY state. Returns the final URL."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data = _vercel_api("GET", f"/v13/deployments/{deployment_id}")
            state = data.get("readyState", "")
            if state == "READY":
                return data.get("url", "")
            if state in ("ERROR", "CANCELED"):
                raise RuntimeError(f"Vercel deployment {deployment_id} reached state {state}")
        except Exception:
            pass
        time.sleep(4)
    raise TimeoutError(f"Vercel deployment {deployment_id} not ready after {timeout_s}s")


@tool
def vercel_deploy_html(html: str, slug: str) -> str:
    """
    Deploy a single HTML file to Vercel and return the live preview URL.

    Use this AFTER calling v0_generate_website() to make the generated website
    instantly accessible to the client — no GitHub Pages, no 75-second wait.

    Args:
        html:  Complete HTML file content (output of v0_generate_website).
        slug:  Short project identifier, e.g. "smith-plumbing-london".
               Used as the Vercel project name: bridge-{slug}.
               Keep it lowercase, hyphens only, under 40 characters.

    Returns:
        Live preview URL like "https://bridge-smith-plumbing-london-xxx.vercel.app"
        or an error string starting with "[" on failure.

    Example:
        html = v0_generate_website(brief="Plumbing landing page, Birmingham...")
        url  = vercel_deploy_html(html=html, slug="smith-plumbing-bham")
        # → "https://bridge-smith-plumbing-bham-abc123.vercel.app"
    """
    if not html or len(html.strip()) < 100:
        return "[vercel_tools error: html is empty or too short]"
    if not slug or len(slug.strip()) < 2:
        return "[vercel_tools error: slug is required, e.g. 'client-name-city']"

    slug = slug.strip().lower().replace(" ", "-")[:40]
    project_name = f"bridge-{slug}"

    # Encode HTML as base64 for the Vercel files payload
    html_b64 = base64.b64encode(html.encode("utf-8")).decode()

    payload = {
        "name": project_name,
        "files": [
            {
                "file": "index.html",
                "data": html_b64,
                "encoding": "base64",
            }
        ],
        "projectSettings": {
            "framework": None,
            "outputDirectory": None,
            "buildCommand": None,
            "installCommand": None,
        },
        "target": "preview",
    }

    try:
        result = _vercel_api("POST", "/v13/deployments", body=payload)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:400]
        except Exception:
            pass
        return f"[vercel_tools HTTP {e.code}: {body}]"
    except ValueError as e:
        return f"[vercel_tools config error: {e}]"
    except Exception as e:
        return f"[vercel_tools error creating deployment: {e}]"

    deployment_id = result.get("id", "")
    url = result.get("url", "")
    ready_state = result.get("readyState", "")

    # If already READY (Vercel often returns this for static deployments), return immediately
    if ready_state == "READY" and url:
        return f"https://{url}"

    # Otherwise poll until ready (static HTML is typically < 10s)
    if deployment_id:
        try:
            final_url = _wait_for_ready(deployment_id, timeout_s=90)
            return f"https://{final_url}" if final_url else f"https://{url}"
        except (TimeoutError, RuntimeError) as e:
            return f"[vercel_tools deployment error: {e}]"

    return f"[vercel_tools error: no deployment ID returned — raw: {str(result)[:200]}]"


@tool
def vercel_list_deployments(project_prefix: str = "bridge-") -> str:
    """
    List recent Vercel deployments whose project name starts with the given prefix.
    Useful for finding existing preview URLs for a client.

    Returns a formatted list of deployments with their URLs and status.
    """
    try:
        data = _vercel_api("GET", f"/v6/deployments?limit=20&projectId={project_prefix}")
        deployments = data.get("deployments", [])
        if not deployments:
            return f"No deployments found with prefix '{project_prefix}'"
        lines = []
        for d in deployments[:10]:
            name  = d.get("name", "?")
            url   = d.get("url", "?")
            state = d.get("readyState", "?")
            lines.append(f"• {name} — https://{url} [{state}]")
        return "\n".join(lines)
    except Exception as e:
        return f"[vercel_tools list error: {e}]"


VERCEL_TOOLS = [vercel_deploy_html, vercel_list_deployments]
