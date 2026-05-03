"""
patch_telegram_personality.py

Injects bot personality headers + Telegram Markdown formatting into all bot n8n workflows.

Changes per bot:
1. "Parse response + risk tag" code node — wraps reply_text with a personality header
   and structures the text for Telegram Markdown (bold section titles, emoji dividers).
2. All "Reply on Telegram" / approval DM / urgent DM HTTP nodes — adds parse_mode: Markdown.

Run: python scripts/patch_telegram_personality.py [--upload]
  --upload   PUT updated workflows to n8n API (requires N8N_BASE_URL + N8N_API_KEY env vars)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

N8N_DIR = Path(__file__).parent.parent / "n8n"

# ── Bot personality registry ──────────────────────────────────────────────────
# icon: the main emoji identity badge shown in every message
# header: the role line under the icon
# colour_emoji: used to decorate section headers
# sign_off: closing line with personality flavour

BOT_PERSONALITIES: dict[str, dict] = {
    "bridge_ceo_bot": {
        "icon": "👑",
        "title": "CEO — Gelson Mascarenhas",
        "header_bar": "━━━━━━━━━━━━━━━━━━━━━━",
        "sign_off": "⚡ Bridge Digital Solution",
        "section_icon": "📌",
        "action_icon": "🎯",
        "alert_icon": "🚨",
        "ok_icon": "✅",
    },
    "bridge_chief_of_staff_bot": {
        "icon": "🗂️",
        "title": "Chief of Staff",
        "header_bar": "──────────────────────",
        "sign_off": "📋 CoS · Bridge OS",
        "section_icon": "🔹",
        "action_icon": "📝",
        "alert_icon": "⚠️",
        "ok_icon": "✔️",
    },
    "bridge_chief_revenue_optimizer_bot": {
        "icon": "💰",
        "title": "Chief Revenue Optimizer",
        "header_bar": "━━━━━━━━━━━━━━━━━━━━━━",
        "sign_off": "📈 CRO · Revenue First",
        "section_icon": "💹",
        "action_icon": "🤑",
        "alert_icon": "🔴",
        "ok_icon": "🟢",
    },
    "bridge_cto_bot": {
        "icon": "🖥️",
        "title": "Chief Technology Officer",
        "header_bar": "──────────────────────",
        "sign_off": "⚙️ CTO · Bridge OS",
        "section_icon": "🔧",
        "action_icon": "🚀",
        "alert_icon": "🔥",
        "ok_icon": "✅",
    },
    "bridge_business_development_bot": {
        "icon": "🤝",
        "title": "BizDev · Business Development",
        "header_bar": "──────────────────────",
        "sign_off": "🌍 BizDev · Bridge OS",
        "section_icon": "🔷",
        "action_icon": "💼",
        "alert_icon": "⚡",
        "ok_icon": "✅",
    },
    "bridge_pm_bot": {
        "icon": "📋",
        "title": "Project Manager",
        "header_bar": "──────────────────────",
        "sign_off": "🗓️ PM · Bridge OS",
        "section_icon": "📌",
        "action_icon": "🏁",
        "alert_icon": "⏰",
        "ok_icon": "✅",
    },
    "bridge_programmer_bot": {
        "icon": "👨‍💻",
        "title": "Programmer",
        "header_bar": "──────────────────────",
        "sign_off": "💾 Programmer · Bridge OS",
        "section_icon": "🔩",
        "action_icon": "🛠️",
        "alert_icon": "🐛",
        "ok_icon": "✅",
    },
    "bridge_chief_sec_off_bot": {
        "icon": "🛡️",
        "title": "Chief Security Officer",
        "header_bar": "━━━━━━━━━━━━━━━━━━━━━━",
        "sign_off": "🔐 CSO · Bridge OS",
        "section_icon": "🔒",
        "action_icon": "🚨",
        "alert_icon": "☠️",
        "ok_icon": "🟢",
    },
    "bridge_security_risk_bot": {
        "icon": "🔍",
        "title": "Security Risk Analyst",
        "header_bar": "──────────────────────",
        "sign_off": "🕵️ SecRisk · Bridge OS",
        "section_icon": "⚠️",
        "action_icon": "🚧",
        "alert_icon": "🔴",
        "ok_icon": "🟡",
    },
    "bridge_finance_bot": {
        "icon": "📊",
        "title": "Finance & Accounting",
        "header_bar": "──────────────────────",
        "sign_off": "💳 Finance · Bridge OS",
        "section_icon": "💵",
        "action_icon": "🏦",
        "alert_icon": "💸",
        "ok_icon": "✅",
    },
    "bridge_researcher_bot": {
        "icon": "🔬",
        "title": "Researcher",
        "header_bar": "──────────────────────",
        "sign_off": "📚 Research · Bridge OS",
        "section_icon": "🧩",
        "action_icon": "💡",
        "alert_icon": "❗",
        "ok_icon": "✅",
    },
    "bridge_cleaner_bot": {
        "icon": "🧹",
        "title": "Cleaner · Inbox & Data Hygiene",
        "header_bar": "──────────────────────",
        "sign_off": "✨ Cleaner · Bridge OS",
        "section_icon": "🗑️",
        "action_icon": "♻️",
        "alert_icon": "🚫",
        "ok_icon": "✅",
    },
}

# ── Formatting code injected at the TOP of each Parse node ───────────────────
# This runs AFTER the existing passthrough logic, wrapping reply_text.

def _format_code(p: dict) -> str:
    """Generate JS snippet that wraps reply_text with bot personality header."""
    icon       = p["icon"]
    title      = p["title"]
    header_bar = p["header_bar"]
    sign_off   = p["sign_off"]
    sect       = p["section_icon"]
    action     = p["action_icon"]
    alert      = p["alert_icon"]
    ok         = p["ok_icon"]

    # Escape backslashes and quotes for embedding in a JS string template
    return f"""
// ── Telegram personality formatting ──────────────────────────────────────────
function _formatTgMsg(raw, taskKind) {{
  if (!raw || raw.length < 3) return raw;

  // Parse ISO timestamp for the footer
  const _ts = new Date().toLocaleTimeString('en-GB', {{hour:'2-digit',minute:'2-digit',timeZone:'UTC'}}) + ' UTC';

  // Build structured header
  const _kind = (taskKind || 'report').replace(/_/g,' ');
  let header = '{icon} *{title}*\\n{header_bar}\\n';

  // Transform the body: bold lines that look like labels, add section icons
  let body = raw
    .replace(/\\*\\*/g, '*')           // normalise bold markers
    .replace(/^(#+)\\s+(.+)$/gm, (_, h, t) => '*' + t + '*')   // ## heading → bold
    .replace(/^[-•]\\s+/gm, '  {sect} ')                        // bullet → section icon
    .replace(/^(\\w[^:\\n]{{2,40}}):\\s*(.+)$/gm, '*$1:* $2')    // Key: val → bold key
    .replace(/ERROR|FAILED|CRITICAL|BLOCKED/g, '{alert} $&')     // flag errors
    .replace(/SUCCESS|COMPLETE|DONE|APPROVED/gi, '{ok} $&')     // flag successes
    .replace(/ACTION:|action:/g, '{action} *ACTION:*')          // highlight actions
    ;

  // Escape Markdown special chars NOT inside existing bold spans
  // (Telegram Markdown v1: escape [ ] only)
  body = body.replace(/([\\[\\]])/g, '\\\\$1');

  const footer = '\\n{header_bar}\\n_{sign_off}_ · _' + _ts + '_';

  return header + body + footer;
}}

// Apply formatting to reply_text if there is one
if ($json && $json.reply_text && $json.reply_text.length > 3) {{
  const _taskKind = $json.task_kind || upstream && upstream.task_kind || '';
  $json.reply_text = _formatTgMsg($json.reply_text, _taskKind);
}}
"""


def _build_new_parse_code(existing_code: str, bot_key: str) -> str:
    """Append personality formatter to existing parse node JS code."""
    p = BOT_PERSONALITIES[bot_key]
    fmt = _format_code(p)
    # Append after the final return statement — wrap original code in a block,
    # then mutate the returned object's reply_text.
    new_code = existing_code.rstrip()
    # Replace the final return line to capture result, format it, then return
    if "return [{ json: {" in new_code:
        # Inject formatter before the last return
        insert_before = new_code.rfind("return [{")
        new_code = (
            new_code[:insert_before]
            + fmt
            + "\n"
            + new_code[insert_before:]
        )
    return new_code


def patch_file(json_path: Path) -> dict | None:
    """Load, patch, return modified workflow dict. Returns None if no changes."""
    bot_key = json_path.stem  # e.g. "bridge_ceo_bot"
    if bot_key not in BOT_PERSONALITIES:
        print(f"  SKIP {bot_key} — no personality defined")
        return None

    p = BOT_PERSONALITIES[bot_key]
    data = json.loads(json_path.read_text(encoding="utf-8"))
    changed = False

    for node in data.get("nodes", []):
        nname = node.get("name", "")
        params = node.get("parameters", {})

        # 1. Patch "Parse response + risk tag" code node
        if nname == "Parse response + risk tag" and "jsCode" in params:
            existing = params["jsCode"]
            if "_formatTgMsg" not in existing:
                params["jsCode"] = _build_new_parse_code(existing, bot_key)
                changed = True
                print(f"  ✅ Patched 'Parse response + risk tag' in {bot_key}")

        # 2. Add parse_mode: Markdown to all Telegram sendMessage HTTP nodes
        if (
            node.get("type") == "n8n-nodes-base.httpRequest"
            and "sendMessage" in str(params.get("url", ""))
            and "telegram" in str(params.get("url", "")).lower()
        ):
            body_str = params.get("jsonBody", "")
            if isinstance(body_str, str) and "parse_mode" not in body_str:
                # Insert parse_mode into the JSON body string
                body_str = body_str.rstrip().rstrip("}")
                body_str += ', "parse_mode": "Markdown" }'
                params["jsonBody"] = body_str
                changed = True
                print(f"  ✅ Added parse_mode:Markdown to '{nname}' in {bot_key}")

    if not changed:
        print(f"  — {bot_key}: nothing to patch")
        return None

    return data


def upload_workflow(data: dict, bot_key: str) -> None:
    base = os.environ.get("N8N_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("N8N_API_KEY", "")
    if not base or not api_key:
        print("  ⚠️  N8N_BASE_URL / N8N_API_KEY not set — skipping upload")
        return

    # Find workflow ID by name
    req = urllib.request.Request(
        f"{base}/api/v1/workflows?limit=250",
        headers={"X-N8N-API-KEY": api_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        workflows = json.loads(resp.read().decode()).get("data", [])

    wf_name = data.get("name", "")
    match = next((w for w in workflows if w.get("name") == wf_name), None)
    if not match:
        print(f"  ⚠️  Workflow '{wf_name}' not found in n8n — skipping upload")
        return

    wf_id = match["id"]
    body = json.dumps(data).encode()
    put_req = urllib.request.Request(
        f"{base}/api/v1/workflows/{wf_id}",
        data=body,
        method="PUT",
        headers={
            "X-N8N-API-KEY": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(put_req, timeout=30) as resp:
            resp.read()
        print(f"  ✅ Uploaded '{wf_name}' (id {wf_id})")
    except Exception as e:
        print(f"  ❌ Upload failed for '{wf_name}': {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true", help="PUT updated workflows to n8n")
    parser.add_argument("--bot", default="", help="Patch only this bot JSON filename")
    args = parser.parse_args()

    files = sorted(N8N_DIR.glob("bridge_*_bot.json"))
    if args.bot:
        files = [f for f in files if f.name == args.bot or f.stem == args.bot]

    if not files:
        print("No matching bot JSON files found.")
        return 1

    for path in files:
        print(f"\n▶ {path.name}")
        patched = patch_file(path)
        if patched is None:
            continue
        # Save back
        path.write_text(json.dumps(patched, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  💾 Saved {path.name}")
        if args.upload:
            upload_workflow(patched, path.stem)

    print("\n✅ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
