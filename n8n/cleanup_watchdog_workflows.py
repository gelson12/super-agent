"""Cleanup: delete orphaned test/watchdog workflows from n8n.

Usage:
  N8N_API_BASE=https://your-n8n.up.railway.app N8N_API_KEY=xxx python cleanup_watchdog_workflows.py

Targets:
  - Watchdog-Health-Test* / Watchdog-CLI-Build-Test* (auto-created test workflows)
  - My workflow* (default n8n template names)
  - *test* / *copy* (likely duplicates)
"""
import json
import os
import urllib.request
import time

BASE = os.environ.get("N8N_API_BASE", "https://outstanding-blessing-production-1d4b.up.railway.app")
KEY = os.environ.get("N8N_API_KEY", "")


def list_all(cursor=None):
    """Fetch all workflows with pagination."""
    url = f"{BASE}/api/v1/workflows?limit=250"
    if cursor:
        url += f"&cursor={cursor}"
    req = urllib.request.Request(
        url,
        headers={"X-N8N-API-KEY": KEY, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    workflows = data.get("data", [])
    next_cursor = data.get("nextCursor")
    if next_cursor:
        workflows.extend(list_all(next_cursor))
    return workflows


def delete_wf(wf_id):
    req = urllib.request.Request(
        f"{BASE}/api/v1/workflows/{wf_id}",
        headers={"X-N8N-API-KEY": KEY},
        method="DELETE",
    )
    urllib.request.urlopen(req, timeout=15)


if __name__ == "__main__":
    print("Fetching all workflows from n8n...")
    workflows = list_all()
    print(f"Total workflows: {len(workflows)}")

    # Junk patterns: watchdog tests, default template names, test workflows, debug tools
    _JUNK_PREFIXES = (
        "Watchdog-Health-Test", "Watchdog-CLI-Build-Test", "My workflow",
        "Test ", "Test-", "Test_",
        "Catch-All ",
    )
    _JUNK_CONTAINS = ("(copy)", " copy ", " test ")
    # Exact names to always delete (known junk)
    _JUNK_EXACT = {
        "Health Monitor - Success Generator",
        "AI Finance Operations Assistant for Small Businesses",
    }
    # Workflows to NEVER delete regardless of pattern match
    _PROTECTED = {
        "Super Agent Chat", "Business Hub", "Daily-SuperAgent-Report",
        "Claude-Verification-Monitor",
    }

    def _is_junk(name: str) -> bool:
        if name in _PROTECTED:
            return False
        if name in _JUNK_EXACT:
            return True
        if name.startswith(_JUNK_PREFIXES):
            return True
        lower = name.lower()
        return any(p in lower for p in _JUNK_CONTAINS)

    watchdog = [w for w in workflows if _is_junk(w["name"])]

    print(f"\nFound {len(watchdog)} junk/test workflows to delete:")
    for w in watchdog:
        print(f"  - {w['name']} (id={w['id']}, active={w.get('active', False)})")

    if not watchdog:
        print("\nNo orphaned watchdog workflows found. n8n is clean.")
    else:
        print(f"\nDeleting {len(watchdog)} workflows...")
        deleted = 0
        errors = 0
        for w in watchdog:
            try:
                delete_wf(w["id"])
                deleted += 1
                print(f"  Deleted: {w['name']}")
                time.sleep(0.2)  # small delay to not hammer the API
            except Exception as e:
                errors += 1
                print(f"  FAILED: {w['name']} - {e}")
        print(f"\nCleanup complete: {deleted} deleted, {errors} errors")

    # Also list remaining workflows for reference
    remaining = [w for w in workflows if w not in watchdog]
    print(f"\nRemaining workflows ({len(remaining)}):")
    for w in remaining:
        status = "ACTIVE" if w.get("active") else "INACTIVE"
        print(f"  [{status}] {w['name']} (id={w['id']})")
