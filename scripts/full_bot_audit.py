"""
Full audit and repair of all 12 Bridge OS bots.
Checks and fixes every known JS/SQL issue in one pass.
Uploads all changed workflows to n8n.
"""
import json, os, re, sys, requests

sys.stdout.reconfigure(encoding="utf-8")

N8N_BASE = "https://outstanding-blessing-production-1d4b.up.railway.app"
N8N_KEY  = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwOTk3N2NkYS05MTQwLTRlNjgt"
            "YTk0OC1lODhkMWY0MDBjMTgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRp"
            "IjoiZWUxMTlkYzItN2MzZi00NTc1LThjMDAtNGM1MWM5NmZhZmEzIiwiaWF0IjoxNzc2NTEw"
            "ODI5LCJleHAiOjE3NzkwNTg4MDB9.Az2G4U7gh55B2MoJK1Ewhk-CNDUqpph-0JYMiSuIWMw")
H = {"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json", "Accept": "application/json"}
BS = chr(92)

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

# Get n8n workflow ID map
resp = requests.get(f"{N8N_BASE}/api/v1/workflows?limit=250", headers=H, timeout=15)
wf_map = {w["name"]: w["id"] for w in resp.json().get("data", [])}

# ── Fix functions ─────────────────────────────────────────────────────────────

def fix1_system_string_to_template(code):
    """Convert const system = \"...\" to backtick template literal.
    Eliminates ALL literal-newline and embedded-double-quote issues at once."""
    if 'const system = `' in code:
        return code, []  # already a template literal
    marker = 'const system = "'
    start = code.find(marker)
    if start == -1:
        return code, []
    q = start + len(marker)
    # Find the intended closing quote by looking for '";' followed by blank line + 'const'
    end_pos = None
    for pattern in ['\";\n\nconst ', '\";\n\n\nconst ', '\";\nconst ']:
        idx = code.find(pattern, q + 50)
        if idx != -1 and (end_pos is None or idx < end_pos):
            end_pos = idx
    if end_pos is None:
        end_pos = code.rfind('";', q, q + 30000)
    if end_pos is None:
        return code, ["WARN: could not find end of system string"]
    content = code[q:end_pos]
    # Check if it has any issues (literal NL or unescaped DQ)
    has_nl = '\n' in content
    # For unescaped DQ: after scanning past escape sequences, find bare "
    i, bare_dq = 0, False
    while i < len(content):
        if content[i] == BS:
            i += 2
        elif content[i] == '"':
            bare_dq = True; break
        else:
            i += 1
    if not has_nl and not bare_dq:
        return code, []  # no issues, leave as-is
    # Convert to template literal
    content_esc = content.replace(BS + '`', '`').replace('`', BS + '`')
    content_esc = content_esc.replace('${', BS + '${')
    new_code = code[:start] + 'const system = `' + content_esc + '`;' + code[end_pos + 2:]
    fixes = []
    if has_nl: fixes.append("fix1a: literal newlines in system string → template literal")
    if bare_dq: fixes.append("fix1b: embedded double-quotes in system string → template literal")
    return new_code, fixes


def fix2_tdz_bug(code):
    """Fix const x = normaliseBrief(x) TDZ bug → const x = normaliseBrief(task.user_text)"""
    pattern = re.compile(r'const\s+(\w+)\s*=\s*\w+\(\s*\1\s*\)')
    def replacer(m):
        var = m.group(1)
        return m.group(0).replace(f'({var})', '(task.user_text)')
    new_code, n = re.subn(pattern, replacer, code)
    if n:
        return new_code, [f"fix2: TDZ self-reference bug fixed ({n} occurrence(s))"]
    return code, []


def fix3_double_comma_sql(sql):
    """Fix ),\n, double-comma in SQL CTEs."""
    fixed = re.sub(r'\)\s*,\s*,\s*', '),\n', sql)
    if fixed != sql:
        return fixed, ["fix3: double-comma CTE separator removed"]
    return sql, []


def fix4_approval_dm_json_injection(body):
    """Fix JSON.stringify(action_payload).slice(0,600) → add .replace to escape DQ."""
    old = "JSON.stringify($json.action_payload).slice(0,600)"
    new = "JSON.stringify($json.action_payload).replace(/\"/g, \"'\").slice(0,600)"
    if old in body:
        return body.replace(old, new), ["fix4: approval DM JSON injection — escaped embedded quotes"]
    return body, []


def fix5_responsmode(params):
    """Fix responseMode responseNode → lastNode (CRO webhook)."""
    if params.get("responseMode") == "responseNode":
        params["responseMode"] = "lastNode"
        return True, ["fix5: responseMode responseNode → lastNode"]
    return False, []


# ── Main audit loop ───────────────────────────────────────────────────────────

total_bots_fixed = 0

for fn in BOT_FILES:
    path = os.path.join("n8n", fn)
    with open(path, encoding="utf-8") as f:
        d = json.load(f)

    all_fixes = []
    for node in d["nodes"]:
        name = node.get("name", "")
        params = node.get("parameters", {})

        # Assemble prompt node — JS fixes
        if name == "Assemble prompt":
            code = params.get("jsCode", "")
            code, f1 = fix1_system_string_to_template(code)
            code, f2 = fix2_tdz_bug(code)
            if f1 or f2:
                params["jsCode"] = code
                all_fixes.extend(f1 + f2)

        # Execute low-risk action — SQL fixes
        if name == "Execute low-risk action":
            sql = params.get("query", "")
            sql, f3 = fix3_double_comma_sql(sql)
            if f3:
                params["query"] = sql
                all_fixes.extend(f3)

        # Medium-risk approval DM — JSON injection fix
        if "medium" in name.lower() or "approval" in name.lower():
            body = params.get("jsonBody", "")
            body, f4 = fix4_approval_dm_json_injection(body)
            if f4:
                params["jsonBody"] = body
                all_fixes.extend(f4)

        # Webhook node — responseMode fix
        if node.get("type") == "n8n-nodes-base.webhook" and name == "Webhook: invoke":
            _, f5 = fix5_responsmode(params)
            if _:
                all_fixes.extend(f5)

    if all_fixes:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"\n{fn}:")
        for fix in all_fixes:
            print(f"  ✓ {fix}")
        wf_id = wf_map.get(d.get("name", ""))
        if wf_id:
            put_data = {k: v for k, v in d.items() if k not in ("id", "active")}
            r = requests.put(f"{N8N_BASE}/api/v1/workflows/{wf_id}", json=put_data, headers=H, timeout=30)
            st = "uploaded OK" if r.status_code < 400 else f"UPLOAD FAILED {r.status_code}: {r.text[:100]}"
            print(f"  → n8n: {st}")
        else:
            print(f"  → WARNING: workflow not found in n8n (name mismatch?)")
        total_bots_fixed += 1
    else:
        print(f"{fn}: clean — no issues found")

print(f"\n{'='*60}")
print(f"Audit complete. {total_bots_fixed}/{len(BOT_FILES)} bots needed fixes.")
