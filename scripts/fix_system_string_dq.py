"""
Fix embedded unescaped double-quotes in const system = "..." strings.
Converts them to template literals (backticks) which don't need DQ escaping.
"""
import json, sys, requests

N8N_BASE = "https://outstanding-blessing-production-1d4b.up.railway.app"
N8N_KEY  = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwOTk3N2NkYS05MTQwLTRlNjgt"
            "YTk0OC1lODhkMWY0MDBjMTgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRp"
            "IjoiZWUxMTlkYzItN2MzZi00NTc1LThjMDAtNGM1MWM5NmZhZmEzIiwiaWF0IjoxNzc2NTEw"
            "ODI5LCJleHAiOjE3NzkwNTg4MDB9.Az2G4U7gh55B2MoJK1Ewhk-CNDUqpph-0JYMiSuIWMw")
H = {"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json", "Accept": "application/json"}

WF_IDS = {
    "bridge_business_development_bot.json": "ptf7UNqQKpiIj7IG",
    "bridge_chief_revenue_optimizer_bot.json": "0S3Jb1UQZNtSqsI5",
    "bridge_cto_bot.json": "EOYTWzQZQZTfTsU4",
}


def convert_system_to_template(code):
    marker = 'const system = "'
    start = code.find(marker)
    if start == -1:
        return code, False
    q = start + len(marker)
    # Find the intended end: look for '";' followed by a blank line + 'const '
    search_from = q + 100
    end_marker_pos = None
    for pattern in ['\";\n\nconst ', '\";\n\n\nconst ', '\";\nconst ']:
        idx = code.find(pattern, search_from)
        if idx != -1 and (end_marker_pos is None or idx < end_marker_pos):
            end_marker_pos = idx
    if end_marker_pos is None:
        # fallback: last '";' within reasonable range
        idx = code.rfind('";', q, q + 20000)
        if idx != -1:
            end_marker_pos = idx
    if end_marker_pos is None:
        print("  Could not find end of system string")
        return code, False

    content = code[q:end_marker_pos]
    # Escape backticks and ${ in content for use inside template literal
    content_esc = content.replace("\\`", "`")  # unescape any already-escaped backticks
    content_esc = content_esc.replace("`", "\\`")
    content_esc = content_esc.replace("${", "\\${")
    new_code = code[:start] + "const system = `" + content_esc + "`;" + code[end_marker_pos + 2:]
    return new_code, True


for fn, wf_id in WF_IDS.items():
    path = "n8n/" + fn
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    changed = False
    for node in d["nodes"]:
        if node.get("name") != "Assemble prompt":
            continue
        code = node["parameters"].get("jsCode", "")
        new_code, ok = convert_system_to_template(code)
        if ok:
            node["parameters"]["jsCode"] = new_code
            changed = True
            print(f"{fn}: converted system string to template literal")
        else:
            print(f"{fn}: no system string found or conversion failed")
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        put_data = {k: v for k, v in d.items() if k not in ("id", "active")}
        r = requests.put(f"{N8N_BASE}/api/v1/workflows/{wf_id}", json=put_data, headers=H, timeout=30)
        status = "OK" if r.status_code < 400 else f"FAIL {r.status_code}: {r.text[:150]}"
        print(f"  -> n8n: {status}")

print("\nDone.")
