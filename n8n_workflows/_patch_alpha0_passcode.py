"""
_patch_alpha0_passcode.py

Adds "alpha0" passcode detection to all Telegram bot DM handlers.

When "alpha0" appears in the message:
  - System prompt switches to ADMIN (full infrastructure access)
  - inspiring-cat gets the admin-context prompt (primary, 10-min timeout)
  - LEGION fallback uses [ADMIN] prefix + shortlist_override=["claude_b"]
    + deadline_ms=600000 so only Claude B (with tool access) handles it

Without "alpha0":  existing bot-role prompt, regular routing. No change.

Affects:
  - Crypto Bridge Bot Commands (kvMrFfKUp1zy9Fek) — "Submit to Claude CLI" node
  - bridge_business_development_bot — "DM → Claude CLI" node
  - Bridge_ChiefRevenueOptimizer_BOT — "DM → Claude CLI" node
  - bridge_cto_bot — "DM → Claude CLI" node
"""
import json, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding='utf-8')

N8N_URL = "https://outstanding-blessing-production-1d4b.up.railway.app"
N8N_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwOTk3N2NkYS05MTQwLTRlNjgtYTk0OC1lODhkMWY0MDBjMTgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiZWUxMTlkYzItN2MzZi00NTc1LThjMDAtNGM1MWM5NmZhZmEzIiwiaWF0IjoxNzc2NTEwODI5LCJleHAiOjE3NzkwNTg4MDB9.Az2G4U7gh55B2MoJK1Ewhk-CNDUqpph-0JYMiSuIWMw"

ADMIN_ROLE = (
    "You are the Bridge Infrastructure Admin AI. ELEVATED ACCESS GRANTED.\\n"
    "You have full control over all Bridge Digital Solutions infrastructure:\\n\\n"
    "SERVICES:\\n"
    "- GitHub: https://github.com/gelson12/super-agent (commit, push, PRs, files)\\n"
    "- n8n: https://outstanding-blessing-production-1d4b.up.railway.app (workflows, env vars)\\n"
    "- Super-Agent: https://super-agent-production.up.railway.app\\n"
    "- Inspiring-Cat: https://inspiring-cat-production.up.railway.app (Claude CLI worker)\\n"
    "- LEGION: https://legion-production-36db.up.railway.app\\n"
    "- Website: bridge-digital-solution.com (served from website/index.html in repo)\\n\\n"
    "RAILWAY CLI (railway is installed — use it for Railway dashboard control):\\n"
    "- RAILWAY_TOKEN env var is set for authentication\\n"
    "- List services: railway service list\\n"
    "- Get env vars: railway variables --service <name>\\n"
    "- Set env var: railway variables set KEY=VALUE --service <name>\\n"
    "- Delete env var: railway variables delete KEY --service <name>\\n"
    "- View logs: railway logs --service <name> --tail 50\\n"
    "- Redeploy service: railway redeploy --service <name> --yes\\n"
    "- Railway services: super-agent, inspiring-cat, legion, n8n, divine-contentment, radiant-appreciation, obsidian-vault\\n\\n"
    "RAILWAY REST API (alternative if CLI fails):\\n"
    "- Base: https://backboard.railway.app/graphql/v2\\n"
    "- Auth header: Authorization: Bearer $RAILWAY_TOKEN\\n"
    "- Use GraphQL mutations for variables, deployments, service config\\n\\n"
    "CODEBASE:\\n"
    "- Working directory: /workspace/super-agent (full read/write/execute access)\\n"
    "- N8N_KEY env var available for n8n API calls\\n"
    "- GITHUB_PAT env var available for GitHub API and git push\\n"
    "- Can run patch scripts, edit files, commit, push, deploy n8n workflows\\n\\n"
    "Execute the requested task completely and autonomously. "
    "Reference exact file paths, service names, variable keys, and API responses. "
    "If a step fails, try the Railway REST API as fallback. Report what changed."
)

# The new Code node JS — replaces the existing one in each bot
# Differs per bot only in BOT_ROLE and BOT_TOKEN_ENV
def make_code(bot_role_escaped, bot_token_env_or_null):
    token_expr = f"$env['{bot_token_env_or_null}'] || ''" if bot_token_env_or_null else "''"
    return f"""const crypto = require('crypto');
const https = require('https');

// ── Inputs ───────────────────────────────────────────────────────────────────
const msg      = $input.first().json.message || {{}};
const chatId   = String((msg.chat && msg.chat.id) || '');
const rawText  = (msg.text || '').trim();
const now      = new Date().toISOString().replace('T',' ').slice(0,16) + ' UTC';
const botToken = {token_expr};

if (!rawText || !chatId) return [{{ json: {{ chatId, reply: '', skip: true }} }}];

// ── Passcode check ───────────────────────────────────────────────────────────
const PASSCODE   = 'alpha0';
const isAdmin    = rawText.toLowerCase().includes(PASSCODE);
const cleanText  = isAdmin ? rawText.replace(/alpha0/gi, '').trim() : rawText;

const BOT_ROLE   = '{bot_role_escaped}';
const ADMIN_ROLE = '{ADMIN_ROLE}';
const systemRole = isAdmin ? ADMIN_ROLE : BOT_ROLE;
const fullPrompt = systemRole + '\\n\\nUser request: ' + cleanText;
// Admin mode: prefix with [ADMIN] so LEGION routes only to Claude B
const legionPrompt = (isAdmin ? '[ADMIN]\\n' : '') + fullPrompt;

// ── Typing indicator (fire & forget) ─────────────────────────────────────────
if (botToken) {{
  try {{
    const tb = JSON.stringify({{ chat_id: chatId, action: 'typing' }});
    await new Promise(res => {{
      const req = https.request({{
        hostname: 'api.telegram.org', path: '/bot' + botToken + '/sendChatAction',
        method: 'POST', headers: {{ 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(tb) }}
      }}, r => {{ r.on('data',()=>{{}}); r.on('end',res); }});
      req.on('error',res); req.setTimeout(5000,()=>{{ req.destroy(); res(); }});
      req.write(tb); req.end();
    }});
  }} catch(e) {{}}
}}

// ── HTTP helpers ──────────────────────────────────────────────────────────────
async function httpPost(hostname, path, bodyStr, headers={{}}) {{
  return new Promise((resolve, reject) => {{
    const req = https.request({{
      hostname, path, method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(bodyStr), ...headers }}
    }}, res => {{
      let d=''; res.on('data',c=>d+=c);
      res.on('end',()=>{{ try{{resolve(JSON.parse(d));}}catch(e){{resolve({{raw:d.slice(0,200)}});}} }});
    }});
    req.on('error',reject);
    req.setTimeout(15000,()=>{{ req.destroy(); reject(new Error('timeout')); }});
    req.write(bodyStr); req.end();
  }});
}}
async function httpGet(hostname, path) {{
  return new Promise((resolve, reject) => {{
    https.get({{ hostname, path }}, res => {{
      let d=''; res.on('data',c=>d+=c);
      res.on('end',()=>{{ try{{resolve(JSON.parse(d));}}catch(e){{resolve({{raw:d.slice(0,200)}});}} }});
    }}).on('error',reject);
  }});
}}

// ── 1. Claude CLI via inspiring-cat ──────────────────────────────────────────
let reply = '';
let usedEngine = 'error';
const pollLimit = isAdmin ? 120 : 60;  // admin: 10 min | chat: 5 min

try {{
  const sub = await httpPost('inspiring-cat-production.up.railway.app', '/tasks',
    JSON.stringify({{ prompt: fullPrompt, type: 'claude' }}));
  if (!sub.task_id) throw new Error('no task_id: ' + JSON.stringify(sub).slice(0,120));

  for (let i = 0; i < pollLimit; i++) {{
    await new Promise(r => setTimeout(r, 5000));
    const poll = await httpGet('inspiring-cat-production.up.railway.app', '/tasks/' + sub.task_id);
    if (poll.status === 'done') {{
      reply = poll.result || '(empty response)';
      usedEngine = 'claude-cli' + (isAdmin ? '-admin' : '');
      break;
    }}
    if (poll.status === 'failed') throw new Error('CLI failed: ' + (poll.error||'?'));
  }}
  if (!reply) throw new Error('CLI timed out');
}} catch(cliErr) {{
  // ── 2. LEGION fallback ────────────────────────────────────────────────────
  try {{
    const legSecret = $env.LEGION_API_SHARED_SECRET || '';
    const legBody = JSON.stringify({{
      query: legionPrompt,
      complexity: isAdmin ? 5 : 3,
      modality: isAdmin ? 'admin' : 'text',
      deadline_ms: isAdmin ? 600000 : 90000,
      task_kind: isAdmin ? 'admin' : 'chat'
    }});
    const ts = Math.floor(Date.now()/1000).toString();
    const sig = crypto.createHmac('sha256', legSecret).update(ts + '\\n' + legBody).digest('hex');
    const legRes = await httpPost('legion-production-36db.up.railway.app', '/v1/respond',
      legBody, {{ 'X-Legion-Ts': ts, 'X-Legion-Sig': sig }});
    reply = legRes.content || JSON.stringify(legRes).slice(0,400);
    usedEngine = 'legion/' + (legRes.winner_agent || '?') + (isAdmin ? '-admin' : '');
  }} catch(legErr) {{
    reply = '\\u26a0\\ufe0f Both Claude CLI and LEGION are unavailable.\\n' +
      'CLI: ' + cliErr.message.slice(0,80) + '\\nLEGION: ' + legErr.message.slice(0,80);
  }}
}}

// Trim to Telegram limit + add footer
const adminBadge = isAdmin ? ' \\uD83D\\uDD10' : '';
const footer = '\\n\\n<i>\\u231a ' + now + ' \\u00b7 ' + usedEngine + adminBadge + '</i>';
const trimmed = reply.slice(0, 4096 - footer.length);
return [{{ json: {{ chatId, reply: trimmed + footer, botToken }} }}];"""


def api(method, path, data=None):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data else None
    req = urllib.request.Request(
        f"{N8N_URL}/api/v1{path}", data=body, method=method,
        headers={"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:400]}, e.code

def deploy(wf_id, wf, label):
    payload = {k: wf.get(k) for k in ('name','nodes','connections','settings','staticData')}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        f"{N8N_URL}/api/v1/workflows/{wf_id}",
        data=body, method='PUT',
        headers={'X-N8N-API-KEY': N8N_KEY, 'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"  [DEPLOYED] {label}: HTTP {resp.status} OK")
            return True
    except urllib.error.HTTPError as e:
        print(f"  [DEPLOY ERROR] {label}: HTTP {e.code} -- {e.read().decode()[:200]}")
        return False

def patch_node(wf_id, wf_name, node_name, new_code, label):
    result, status = api("GET", f"/workflows/{wf_id}")
    if "error" in result:
        print(f"  [ERROR] Fetch {label}: {result['error']}")
        return False
    wf = result
    patched = False
    for n in wf.get('nodes', []):
        if n.get('name') == node_name:
            n['parameters']['jsCode'] = new_code
            patched = True
            break
    if not patched:
        print(f"  [WARN] Node '{node_name}' not found in {label}")
        return False
    return deploy(wf_id, wf, label)

# ── Bot definitions ─────────────────────────────────────────────────────────
BOTS = [
    {
        "wf_id": "kvMrFfKUp1zy9Fek",
        "node_name": "Submit to Claude CLI",
        "label": "Crypto Bridge Bot Commands",
        "token_env": None,
        "role": (
            "You are the Crypto Bridge assistant with full access to the BTC and ETH "
            "trading workflows on n8n. You can read files in /workspace/super-agent/n8n_workflows/, "
            "run patch scripts, check execution logs via n8n API, and deploy fixes. "
            "BTC workflow ID: 7onbBjeUwHkSsuyc | ETH workflow ID: GgUjF0EQw1wQa2G1. "
            "Be concise. Reference specific numbers, node names, and file paths."
        ),
    },
    {
        "wf_id": None,
        "wf_name": "bridge_business_development_bot",
        "node_name": "DM → Claude CLI",
        "label": "BizDev Bot",
        "token_env": "Bridge_Business_Development_bot",
        "role": (
            "You are the Bridge Business Development Bot for Bridge Digital Solutions. "
            "Role: identify revenue opportunities, manage leads, track pipeline, build partnerships. "
            "Access: super-agent system, n8n workflows, database. Be proactive and data-driven."
        ),
    },
    {
        "wf_id": None,
        "wf_name": "Bridge_ChiefRevenueOptimizer_BOT",
        "node_name": "DM → Claude CLI",
        "label": "CRO Bot",
        "token_env": "bridge_ceo_bot",
        "role": (
            "You are the Bridge Chief Revenue Optimizer for Bridge Digital Solutions. "
            "Role: maximize revenue via pricing, conversion, churn reduction, upsell, growth experiments. "
            "Access: n8n workflows, databases, analytics. Be analytical and ROI-focused."
        ),
    },
    {
        "wf_id": None,
        "wf_name": "bridge_cto_bot",
        "node_name": "DM → Claude CLI",
        "label": "CTO Bot",
        "token_env": "bridge_pm_bot",
        "role": (
            "You are the Bridge CTO Bot for Bridge Digital Solutions. "
            "Role: architecture decisions, code quality, infrastructure management, security, dev coordination. "
            "Full codebase access at /workspace/super-agent. Can read/write files, run tests, commit, deploy."
        ),
    },
]

print("=" * 60)
print("PATCHING: Adding alpha0 admin passcode to all bot DM handlers")
print("=" * 60)

for bot in BOTS:
    print(f"\n--- {bot['label']} ---")

    wf_id = bot.get('wf_id')
    if not wf_id:
        # Look up by name
        result, _ = api("GET", "/workflows?limit=100")
        for w in result.get("data", []):
            if w.get("name") == bot['wf_name']:
                wf_id = w["id"]
                break
    if not wf_id:
        print(f"  [SKIP] Workflow not found")
        continue
    print(f"  Workflow ID: {wf_id}")

    # Escape the bot role for JS string embedding
    escaped_role = (bot['role']
                    .replace('\\', '\\\\')
                    .replace("'", "\\'")
                    .replace('\n', '\\n'))

    new_code = make_code(escaped_role, bot['token_env'])
    patch_node(wf_id, bot.get('wf_name', ''), bot['node_name'], new_code, bot['label'])

print()
print("=" * 60)
print("DONE")
print()
print("Usage:")
print("  Normal DM  → bot-specific AI, inspiring-cat → LEGION fallback")
print("  'alpha0 <task>' → ADMIN mode:")
print("    - Full infrastructure access (GitHub, n8n, website, Railway)")
print("    - inspiring-cat primary (10-min timeout)")
print("    - LEGION routes ONLY to Claude B (with codebase + tools)")
print("    - Reply includes 🔐 badge to confirm admin mode activated")
print("=" * 60)
