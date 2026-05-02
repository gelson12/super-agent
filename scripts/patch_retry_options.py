"""
Bridge OS — Patch retry options on all bot HTTP Request nodes that call super-agent.
Adds retryOnFail=true, maxTries=3, waitBetweenTries=4000 to the node so Railway
cold-starts (5-10s) don't cause silent workflow failures.

Usage:
  python scripts/patch_retry_options.py           # dry run — show what would change
  python scripts/patch_retry_options.py --upload  # patch + upload to n8n API
"""
import json
import os
import sys
import argparse

N8N_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "n8n")

BOT_FILES = [
    "bridge_ceo_bot.json",
    "bridge_chief_of_staff_bot.json",
    "bridge_chief_revenue_optimizer_bot.json",
    "bridge_chief_sec_off_bot.json",
    "bridge_cleaner_bot.json",
    "bridge_cto_bot.json",
    "bridge_finance_bot.json",
    "bridge_pm_bot.json",
    "bridge_programmer_bot.json",
    "bridge_researcher_bot.json",
    "bridge_business_development_bot.json",
    "bridge_security_risk_bot.json",
]

RETRY_PATCH = {
    "retryOnFail": True,
    "maxTries": 3,
    "waitBetweenTries": 4000,
    "timeout": 180000,  # 3 minutes — CEO/CRO prompts with full inbox context can take >60s
}


def _find_super_agent_node(nodes):
    """Return the HTTP Request node that calls super-agent, or None."""
    for node in nodes:
        if node.get("type") != "n8n-nodes-base.httpRequest":
            continue
        name = node.get("name", "")
        if "super-agent" in name.lower():
            return node
        # Fallback: check URL
        url = node.get("parameters", {}).get("url", "")
        if "super-agent" in url:
            return node
    return None


def patch_bot(filename):
    path = os.path.join(N8N_DIR, filename)
    if not os.path.exists(path):
        return {"changed": False, "node_name": None, "error": f"File not found: {path}", "data": None}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    node = _find_super_agent_node(data.get("nodes", []))
    if not node:
        return {"changed": False, "node_name": None, "error": "No super-agent HTTP node found", "data": None}

    params = node.setdefault("parameters", {})
    options = params.setdefault("options", {})

    already_done = (
        options.get("retryOnFail") is True
        and options.get("maxTries") == 3
        and options.get("timeout") == 180000
    )
    # Merge — don't overwrite options already present (e.g. neverError)
    options.update(RETRY_PATCH)

    if not already_done:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return {"changed": not already_done, "node_name": node.get("name"), "error": None, "data": data}


def upload_to_n8n(workflow_data, filename):
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests not installed"}

    base = os.environ.get("N8N_BASE_URL", "").rstrip("/")
    key  = os.environ.get("N8N_API_KEY", "")
    if not base or not key:
        # Try reading from .env file
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            with open(env_path) as ef:
                for line in ef:
                    line = line.strip()
                    if line.startswith("N8N_BASE_URL="):
                        base = line.split("=", 1)[1].rstrip("/")
                    elif line.startswith("N8N_API_KEY="):
                        key = line.split("=", 1)[1]
    if not base or not key:
        return {"ok": False, "error": "N8N_BASE_URL or N8N_API_KEY not set"}

    headers = {"X-N8N-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"}
    wf_name = workflow_data.get("name", "")

    resp = requests.get(f"{base}/api/v1/workflows?limit=250", headers=headers, timeout=15)
    if resp.status_code >= 400:
        return {"ok": False, "error": f"List failed: {resp.status_code}"}

    wf_id = next((w["id"] for w in resp.json().get("data", []) if w.get("name") == wf_name), None)
    if not wf_id:
        return {"ok": False, "error": f"Workflow '{wf_name}' not found in n8n"}

    # Strip read-only top-level id before PUT
    put_data = {k: v for k, v in workflow_data.items() if k not in ("id", "active")}
    put = requests.put(f"{base}/api/v1/workflows/{wf_id}", json=put_data, headers=headers, timeout=30)
    if put.status_code >= 400:
        return {"ok": False, "error": f"PUT {put.status_code}: {put.text[:200]}"}

    return {"ok": True, "workflow_id": wf_id}


def main():
    parser = argparse.ArgumentParser(description="Add retry options to Bridge bot HTTP nodes")
    parser.add_argument("--upload", action="store_true", help="Upload patched workflows to n8n")
    args = parser.parse_args()

    print(f"Bridge retry patcher — {len(BOT_FILES)} bots\n")
    errors = 0
    patched = 0

    for filename in BOT_FILES:
        result = patch_bot(filename)

        if result["error"]:
            print(f"  ERROR    {filename}: {result['error']}")
            errors += 1
            continue

        status = "PATCHED" if result["changed"] else "already ok"
        print(f"  {status:<10} {filename}  (node: {result['node_name']})")

        if result["changed"]:
            patched += 1
            if args.upload:
                up = upload_to_n8n(result["data"], filename)
                if up["ok"]:
                    print(f"             -> uploaded (id: {up['workflow_id']})")
                else:
                    print(f"             -> UPLOAD FAILED: {up['error']}")
                    errors += 1

    print(f"\nDone. {patched} patched, {errors} errors.")
    sys.exit(errors)


if __name__ == "__main__":
    main()
