"""
Bridge OS — Migrate bot workflows from /chat/direct to /webhook/bot-engine.

Changes per bot:
1. Replace "super-agent /chat/direct" HTTP Request node with bot-engine node
2. Replace "Parse response + risk tag" Code node with a lightweight passthrough
   (bot-engine handles parse/validate/risk — passthrough just re-emits fields)
3. Remove perf_self_report CTE from "Execute low-risk action" Postgres node
4. Rename connection key in workflow connections dict

Usage:
  python scripts/patch_bot_engine.py                       # dry run — show what would change
  python scripts/patch_bot_engine.py --bot bridge_pm_bot.json  # single bot
  python scripts/patch_bot_engine.py --upload              # patch all + upload to n8n
  python scripts/patch_bot_engine.py --bot bridge_pm_bot.json --upload  # single bot + upload
"""
import json
import os
import re
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

# Security bot has custom parse logic with 3-tier risk overrides — skip parse node replacement
_SKIP_PARSE_REPLACE = {"bridge_security_risk_bot.json"}

_CHAT_DIRECT_NAME = "super-agent /chat/direct"
_BOT_ENGINE_NAME  = "super-agent /webhook/bot-engine"
_PARSE_NODE_NAME  = "Parse response + risk tag"
_EXEC_LOW_RISK    = "Execute low-risk action"

_BOT_ENGINE_URL = (
    "={{ $env.SUPER_AGENT_URL || 'https://super-agent-production.up.railway.app' }}/webhook/bot-engine"
)

_PASSTHROUGH_JS = (
    "// Passthrough — bot-engine already parsed, validated, and risk-annotated the response.\n"
    "const _upstream = $('Assemble prompt').first().json;\n"
    "if (_upstream && _upstream._pre_formed) {\n"
    "    let _pfActions;\n"
    "    try { _pfActions = JSON.parse(_upstream.message).actions; } catch(e) { _pfActions = []; }\n"
    "    return [{ json: {\n"
    "        reply_text: '',\n"
    "        actions: [{ type: 'no_op', risk: 'low',\n"
    "            payload: (_pfActions[0] && _pfActions[0].payload) || { reason: 'loop_guard' } }],\n"
    "        bot_name: _upstream.bot_name, user_chat_id: _upstream.user_chat_id,\n"
    "        task_kind: 'loop_escalation', model_used: 'loop_guard', parse_error: null\n"
    "    }}];\n"
    "}\n"
    "const upstream = $('Assemble prompt').first().json;\n"
    "return [{ json: {\n"
    "    reply_text:  $json.reply_text  || '',\n"
    "    actions:     Array.isArray($json.actions) ? $json.actions : [],\n"
    "    model_used:  $json.model_used  || 'unknown',\n"
    "    parse_error: $json.parse_error || null,\n"
    "    bot_name:    upstream.bot_name,\n"
    "    user_chat_id: upstream.user_chat_id,\n"
    "    task_kind:   upstream.task_kind,\n"
    "} }];"
)


def _build_bot_engine_node(original_node: dict) -> dict:
    """Return a replacement HTTP Request node pointing to /webhook/bot-engine."""
    return {
        "id":          original_node["id"],
        "name":        _BOT_ENGINE_NAME,
        "type":        "n8n-nodes-base.httpRequest",
        "typeVersion": original_node.get("typeVersion", 4.2),
        "position":    original_node.get("position", [1560, 400]),
        "parameters": {
            "method": "POST",
            "url": _BOT_ENGINE_URL,
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "Content-Type", "value": "application/json"}
                ]
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "bot_name": {{ JSON.stringify($json.bot_name || "unknown") }}, '
                '"task_block": {{ JSON.stringify($json.message || "") }}, '
                '"session_id": {{ JSON.stringify($json.session_id || "default") }}, '
                '"api_key": {{ JSON.stringify($env.N8N_API_KEY || "") }} }'
            ),
            "options": {
                "timeout": 60000,
                "response": {"response": {"neverError": True}},
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 4000,
            },
        },
    }


def _remove_perf_cte(query: str) -> str:
    """Strip the perf_self_report CTE and its SELECT reference from an SQL string."""
    # Remove the CTE block: ",\nperf_self_report AS (\n  ...\n)"
    query = re.sub(
        r",\s*perf_self_report\s+AS\s*\(\s*INSERT\s+INTO\s+bridge\.agent_performance.*?RETURNING\s+agent_name\s*\)",
        "",
        query,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove SELECT reference to perf_self_report
    query = re.sub(
        r",?\s*\(SELECT agent_name FROM perf_self_report\)\s*AS\s*perf_recorded",
        "",
        query,
        flags=re.IGNORECASE,
    )
    return query


def patch_bot(filename: str) -> dict:
    path = os.path.join(N8N_DIR, filename)
    if not os.path.exists(path):
        return {"changed": False, "steps": [], "error": f"File not found: {path}", "data": None}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = data.get("nodes", [])
    connections = data.get("connections", {})
    steps = []
    changed = False

    for i, node in enumerate(nodes):
        name = node.get("name", "")

        # Step 1: Replace /chat/direct → /webhook/bot-engine
        if name == _CHAT_DIRECT_NAME:
            nodes[i] = _build_bot_engine_node(node)
            steps.append("replaced /chat/direct node with /webhook/bot-engine")
            changed = True

        # Step 2: Replace parse node (unless in skip list)
        elif name == _PARSE_NODE_NAME and filename not in _SKIP_PARSE_REPLACE:
            node["parameters"]["jsCode"] = _PASSTHROUGH_JS
            steps.append("replaced Parse response node with passthrough")
            changed = True

        # Step 3: Remove perf_self_report CTE
        elif name == _EXEC_LOW_RISK and node.get("type") == "n8n-nodes-base.postgres":
            original_q = node["parameters"].get("query", "")
            new_q = _remove_perf_cte(original_q)
            if new_q != original_q:
                node["parameters"]["query"] = new_q
                steps.append("removed perf_self_report CTE from Execute low-risk action")
                changed = True

    # Step 4: Rename connections key
    if _CHAT_DIRECT_NAME in connections:
        connections[_BOT_ENGINE_NAME] = connections.pop(_CHAT_DIRECT_NAME)
        steps.append("renamed connections key to /webhook/bot-engine")
        changed = True

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return {"changed": changed, "steps": steps, "error": None, "data": data}


def upload_to_n8n(workflow_data: dict, filename: str) -> dict:
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests not installed"}

    base = os.environ.get("N8N_BASE_URL", "").rstrip("/")
    key  = os.environ.get("N8N_API_KEY", "")
    if not base or not key:
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

    put_data = {k: v for k, v in workflow_data.items() if k not in ("id", "active")}
    put = requests.put(f"{base}/api/v1/workflows/{wf_id}", json=put_data, headers=headers, timeout=30)
    if put.status_code >= 400:
        return {"ok": False, "error": f"PUT {put.status_code}: {put.text[:200]}"}

    return {"ok": True, "workflow_id": wf_id}


def main():
    parser = argparse.ArgumentParser(description="Migrate Bridge bots to /webhook/bot-engine")
    parser.add_argument("--bot",    help="Patch a single bot JSON file only")
    parser.add_argument("--upload", action="store_true", help="Upload patched workflows to n8n")
    args = parser.parse_args()

    targets = [args.bot] if args.bot else BOT_FILES
    print(f"Bridge bot-engine patcher — {len(targets)} bot(s)\n")
    errors = 0
    patched = 0

    for filename in targets:
        result = patch_bot(filename)

        if result["error"]:
            print(f"  ERROR    {filename}: {result['error']}")
            errors += 1
            continue

        if result["changed"]:
            print(f"  PATCHED  {filename}")
            for step in result["steps"]:
                print(f"           • {step}")
            patched += 1
            if args.upload:
                up = upload_to_n8n(result["data"], filename)
                if up["ok"]:
                    print(f"           -> uploaded (id: {up['workflow_id']})")
                else:
                    print(f"           -> UPLOAD FAILED: {up['error']}")
                    errors += 1
        else:
            print(f"  ok       {filename}  (already migrated or no matching nodes)")

    print(f"\nDone. {patched} patched, {errors} errors.")
    sys.exit(errors)


if __name__ == "__main__":
    main()
