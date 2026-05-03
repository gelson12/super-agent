"""
Patch all bot HTTP Request nodes calling /webhook/bot-engine to include
task_kind in the body, derived from the task variable in each bot's payload.
scheduled_tick tasks will route to Haiku (cheap), others to Sonnet.
"""
import json, os, sys, requests

N8N_BASE = "https://outstanding-blessing-production-1d4b.up.railway.app"
N8N_KEY  = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwOTk3N2NkYS05MTQwLTRlNjgt"
            "YTk0OC1lODhkMWY0MDBjMTgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRp"
            "IjoiZWUxMTlkYzItN2MzZi00NTc1LThjMDAtNGM1MWM5NmZhZmEzIiwiaWF0IjoxNzc2NTEw"
            "ODI5LCJleHAiOjE3NzkwNTg4MDB9.Az2G4U7gh55B2MoJK1Ewhk-CNDUqpph-0JYMiSuIWMw")
H = {"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json", "Accept": "application/json"}

BOT_FILES = [
    "bridge_ceo_bot.json", "bridge_chief_of_staff_bot.json",
    "bridge_chief_revenue_optimizer_bot.json", "bridge_chief_sec_off_bot.json",
    "bridge_cleaner_bot.json", "bridge_cto_bot.json", "bridge_finance_bot.json",
    "bridge_pm_bot.json", "bridge_programmer_bot.json", "bridge_researcher_bot.json",
    "bridge_business_development_bot.json", "bridge_security_risk_bot.json",
]

resp = requests.get(f"{N8N_BASE}/api/v1/workflows?limit=250", headers=H, timeout=15)
wf_map = {w["name"]: w["id"] for w in resp.json().get("data", [])}

for fn in BOT_FILES:
    path = os.path.join("n8n", fn)
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    changed = False
    for node in d["nodes"]:
        if node.get("type") != "n8n-nodes-base.httpRequest":
            continue
        if "bot-engine" not in node.get("name", "").lower():
            continue
        body = node.get("parameters", {}).get("body", {})
        params = body.get("parameters", []) if isinstance(body, dict) else []
        # Check if task_kind already present
        if any(p.get("name") == "task_kind" for p in params):
            print(f"{fn}: task_kind already present")
            continue
        # Add task_kind parameter
        params.append({"name": "task_kind", "value": "={{$json.task_kind || 'agent_invoke'}}"})
        changed = True
        print(f"{fn}: added task_kind field")
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        wf_id = wf_map.get(d.get("name", ""))
        if wf_id:
            put_data = {k: v for k, v in d.items() if k not in ("id", "active")}
            r = requests.put(f"{N8N_BASE}/api/v1/workflows/{wf_id}", json=put_data, headers=H, timeout=30)
            st = "OK" if r.status_code < 400 else f"FAIL {r.status_code}"
            print(f"  -> n8n: {st}")

print("\nDone.")
