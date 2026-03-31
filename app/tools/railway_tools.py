"""
Railway tools — Super Agent can inspect and manage its own Railway deployment.

Uses:
  1. Railway GraphQL API (primary) — deployments, variables, services, logs
  2. Railway CLI fallback via subprocess — for operations not in the REST API

All write operations (redeploy, set variable) require the owner safe word,
which the dispatcher enforces before any agent is called.
"""
import os
import subprocess
import shlex
import httpx
from langchain_core.tools import tool
from ..config import settings
from ..cache.tool_cache import cached_tool

_RAILWAY_API = "https://backboard.railway.app/graphql/v2"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.railway_token}",
        "Content-Type": "application/json",
    }


def _gql(query: str, variables: dict = None) -> dict:
    """Execute a Railway GraphQL query. Returns the full response dict."""
    if not settings.railway_token:
        return {"error": "RAILWAY_TOKEN not set"}
    try:
        resp = httpx.post(
            _RAILWAY_API,
            headers=_headers(),
            json={"query": query, "variables": variables or {}},
            timeout=30,
        )
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def _cli(command: str, timeout: int = 30) -> str:
    """Run a railway CLI command and return stdout."""
    try:
        result = subprocess.run(
            shlex.split(f"railway {command}"),
            capture_output=True, text=True, timeout=timeout,
        )
        return (result.stdout or result.stderr or "(no output)").strip()
    except Exception as e:
        return f"[Railway CLI error: {e}]"


# ── Read tools ────────────────────────────────────────────────────────────────

@tool
def railway_list_services(dummy: str = "") -> str:
    """List all services in the Railway project with their status and URLs."""
    data = _gql("""
        query {
          me {
            projects {
              edges {
                node {
                  name
                  services {
                    edges {
                      node {
                        name
                        serviceInstances {
                          edges {
                            node {
                              serviceId
                              environmentId
                              domains {
                                serviceDomains { domain }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
    """)
    if "error" in data:
        return f"[Railway error: {data['error']}]"
    try:
        projects = data["data"]["me"]["projects"]["edges"]
        lines = []
        for p in projects:
            proj = p["node"]
            lines.append(f"Project: {proj['name']}")
            for s in proj["services"]["edges"]:
                svc = s["node"]
                lines.append(f"  Service: {svc['name']}")
        return "\n".join(lines) if lines else "No services found"
    except Exception as e:
        return f"[Railway parse error: {e}] Raw: {data}"


@tool
@cached_tool(ttl=30)
def railway_get_logs(service_name: str = "") -> str:
    """
    Get recent deployment logs from Railway via CLI.
    Optionally filter by service name.
    """
    if not settings.railway_token:
        return "[Railway error: RAILWAY_TOKEN not set]"
    cmd = "logs --tail 50"
    return _cli(cmd, timeout=20)


@tool
@cached_tool(ttl=120)
def railway_list_variables(dummy: str = "") -> str:
    """List all environment variables in the Railway service (names only, not values)."""
    if not settings.railway_token:
        return "[Railway error: RAILWAY_TOKEN not set]"
    result = _cli("variables", timeout=15)
    # Strip actual values — return only variable names for security
    lines = []
    for line in result.splitlines():
        if "=" in line:
            lines.append(line.split("=")[0].strip())
        else:
            lines.append(line)
    return "\n".join(lines) if lines else result


@tool
def railway_get_deployment_status(dummy: str = "") -> str:
    """Get the current deployment status of the Railway service."""
    if not settings.railway_token:
        return "[Railway error: RAILWAY_TOKEN not set]"
    return _cli("status", timeout=15)


# ── Write tools (require owner safe word via dispatcher) ──────────────────────

@tool
def railway_redeploy(service_name: str = "") -> str:
    """
    Trigger a Railway redeploy of the current service.
    Use after committing code fixes to GitHub — Railway will pull the latest commit.
    Requires owner authorization.
    """
    if not settings.railway_token:
        return "[Railway error: RAILWAY_TOKEN not set]"
    return _cli("redeploy --yes", timeout=60)


@tool
def railway_set_variable(name_value: str) -> str:
    """
    Set a Railway environment variable. Format: 'VARIABLE_NAME=value'.
    Takes effect on next deploy. Requires owner authorization.
    Example: 'MAX_TOKENS_CLAUDE=4096'
    """
    if not settings.railway_token:
        return "[Railway error: RAILWAY_TOKEN not set]"
    if "=" not in name_value:
        return "[Railway error: format must be 'NAME=value']"
    name, value = name_value.split("=", 1)
    return _cli(f"variables set {name.strip()}={value.strip()}", timeout=15)
