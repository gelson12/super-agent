"""
Create bridge_finance_bot.json — 24-node Finance agent.
Cloned from CEO bot topology with Finance-specific system prompt, triggers, and queries.
Run: python scripts/create_finance_bot.py
"""
import json
import os
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N8N_DIR = os.path.join(BASE_DIR, "n8n")

# Load CEO bot as structural template
with open(os.path.join(N8N_DIR, "bridge_ceo_bot.json"), encoding="utf-8") as f:
    template = json.load(f)

import copy
finance = copy.deepcopy(template)

finance["name"] = "Bridge_Finance_BOT"

FINANCE_SYSTEM = (
    "You are Bridge_Finance_BOT, the financial intelligence layer of Bridge Digital Solutions. "
    "You are the company's real-time CFO — monitoring costs, revenue, token usage, and agent activity "
    "to give CEO and COS full financial visibility without manual reporting.\n\n"
    "ROLE\n"
    "Track all costs, revenue streams, and token consumption. Produce structured financial reports. "
    "Flag budget anomalies immediately. Calculate ROI per campaign. Ensure the company never runs "
    "blind on spending or cash position.\n\n"
    "DAILY REPORT (07:00 UTC) — send via reply_text:\n"
    "💰 DAILY BRIDGE FINANCE REPORT\n"
    "Date: {date}\n"
    "AI Costs (24h): ${ai_cost}\n"
    "Revenue (24h): ${revenue}\n"
    "Net Margin: {margin}%\n"
    "Agent calls: {agent_calls}\n"
    "Active campaigns: {campaigns}\n"
    "⚠️ Alerts: {alerts}\n\n"
    "WEEKLY P&L SUMMARY (Monday 08:00 UTC) — send to CEO via memo:\n"
    "Include 7-day totals, top 3 cost drivers, best ROI campaign, and budget recommendation.\n\n"
    "BUDGET ALERT THRESHOLD: Flag to CEO immediately if daily AI cost exceeds $5 USD.\n\n"
    "DB QUERIES (read-only):\n"
    "  SELECT * FROM bridge.expenses WHERE created_at >= NOW() - INTERVAL '24h'\n"
    "  SELECT * FROM bridge.billing_records WHERE created_at >= NOW() - INTERVAL '7d'\n"
    "  SELECT bot_name, COUNT(*) as calls FROM bridge.workflow_events "
    "    WHERE created_at >= NOW() - INTERVAL '24h' GROUP BY bot_name\n\n"
    "ENABLED FLAG: Check bridge.system_limits WHERE key = 'finance_bot_enabled' before acting. "
    "If value != 'true', emit no_op with reason='finance_bot_disabled'.\n\n"
    "ESCALATE WHEN: daily AI cost > $5, revenue drops >20% vs prior day, any billing anomaly, "
    "any agent running >3x normal call volume.\n\n"
    "OUTPUT STYLE: Emoji-rich for human-facing Telegram messages. Structured JSON for agent memos. "
    "Never guess figures — only report what you can read from DB queries."
)

GLOBAL_PROTOCOL = """
const globalProtocol = `

GLOBAL OPERATING PROTOCOL (non-negotiable):
1. ALWAYS respond in valid JSON only. No prose, no markdown outside JSON strings.
2. EXACT response structure: {"reply_text": "...", "actions": [...]}
3. reply_text: human-readable Telegram message. Brief, role-appropriate.
4. actions: array of {type, payload} objects. Empty array if no actions.
5. NEVER include PLACEHOLDER, PASTE_, TBD, INSERT_HERE values in any field.
6. NEVER mention internal tools (n8n, shell, SQL, workflow IDs) in reply_text.
7. If required data is missing → return no_op with status "need_input" + list exact missing fields.
8. If blocked → escalate to Chief of Staff. NEVER retry autonomously.
9. MAX_RETRIES = 1. If attempt_count >= 1 in incoming body_json → return no_op immediately.
`;
"""

LOOP_DETECTION_PREFIX = """const _buildTask = $('Build task payload').first().json;
if (_buildTask.loop_detected) {
    return [{ json: {
        message: JSON.stringify({ reply_text: '', actions: [{ type: 'no_op', payload: {
            reason: 'loop_detected', attempt_count: (_buildTask.original_task && _buildTask.original_task.attempt_count) || 1
        }}]}),
        session_id: 'bridge-loop-guard-' + Date.now(),
        task_kind: 'loop_escalation', user_chat_id: null,
        bot_name: _buildTask.bot_name || 'unknown', _pre_formed: true
    }}];
}

"""

ASSEMBLE_CODE = LOOP_DETECTION_PREFIX + """const task = $('Build task payload').first().json;
const inboxRow = $('Fetch open inbox').first().json || {};
const inbox = Array.isArray(inboxRow.open_inbox) ? inboxRow.open_inbox : [];
const ctxRow = $('Fetch bot context').first().json || {};
const ctx = ctxRow.context ? [ctxRow.context] : [ctxRow];
""" + GLOBAL_PROTOCOL + """
const system = """ + json.dumps(FINANCE_SYSTEM) + """;

const contextBlock = JSON.stringify({
    now: task.now_iso,
    bot: task.bot_name,
    task_kind: task.task,
    open_inbox: inbox,
    bot_context: ctx,
    daily_cadence: task.daily_spec,
    weekly_cadence: task.weekly_spec,
}, null, 2);

const taskBlock = task.task === 'user_dm'
    ? `The user sent you this message on Telegram:\\n${task.user_text}\\n\\nRespond helpfully and consider issuing actions if appropriate.`
    : task.task === 'scheduled_tick'
      ? `This is a scheduled cadence run. Produce the appropriate financial report or check for anomalies.`
      : `An inter-agent invocation arrived from '${task.from_agent}': ${task.user_text || 'no message body'}. Decide how to respond.`;

const outputGuard = `\\n\\n[OUTPUT FORMAT]\\nReturn ONLY a JSON object (no prose, no markdown fences):\\n{\\n  "reply_text": "<Telegram message or empty string>",\\n  "actions": [\\n    {"type": "memo",     "payload": {"to_agent": "ceo|chief_of_staff|all", "memo_type": "finance_report|alert|status", "priority": "urgent|high|normal|low", "subject": "...", "body_json": {...}}},\\n    {"type": "query",    "payload": {"sql": "<safe single SELECT>"}},\\n    {"type": "no_op",    "payload": {"reason": "..."}}\\n  ]\\n}\\nKeep actions <= 5.`;

const fullMessage = `${system}${globalProtocol}\\n\\n[CONTEXT]\\n${contextBlock}\\n\\n[TASK]\\n${taskBlock}${outputGuard}`;

return [{ json: {
    message: fullMessage,
    session_id: `bridge-finance-${task.now_iso.slice(0,16).replace(/[:T-]/g,'')}`,
    task_kind: task.task,
    user_chat_id: task.chat_id,
    bot_name: task.bot_name,
} }];"""

PARSE_CODE = """// ── Pre-formed short-circuit (loop guard) ──────────────────────────────────
const _upstream = $('Assemble prompt').first().json;
if (_upstream && _upstream._pre_formed) {
    let _pfActions;
    try { _pfActions = JSON.parse(_upstream.message).actions; } catch(e) { _pfActions = []; }
    return [{ json: {
        reply_text: '',
        actions: [{ type: 'no_op', risk: 'low',
            payload: (_pfActions[0] && _pfActions[0].payload) || { reason: 'loop_guard' } }],
        bot_name: _upstream.bot_name, user_chat_id: _upstream.user_chat_id,
        task_kind: 'loop_escalation', model_used: 'loop_guard', parse_error: null
    }}];
}

const raw = $json.response || '';
const riskMap = { "memo": "low", "archive": "low", "no_op": "low",
                  "escalate": "low", "query": "medium", "cleanup": "high" };
const defaultRisk = 'low';

let parsed = null, parseError = null;
try {
    let cleaned = raw.trim().replace(/^```(?:json)?/, '').replace(/```$/, '').trim();
    parsed = JSON.parse(cleaned);
} catch (e) { parseError = e.message; }
if (!parsed) {
    try { const m = raw.match(/\\{[\\s\\S]*\\}/); if (m) parsed = JSON.parse(m[0]); } catch (e) {}
}

let reply_text = '', actions = [];
if (parsed && typeof parsed === 'object' && parsed.reply_text !== undefined) {
    reply_text = typeof parsed.reply_text === 'string' ? parsed.reply_text : '';
    actions = Array.isArray(parsed.actions) ? parsed.actions.slice(0, 5) : [];
} else {
    reply_text = '';
    actions = [{ type: 'no_op', payload: {
        reason: 'json_parse_failed',
        parse_error: parseError || 'no structured response from model',
        raw_preview: String(raw).slice(0, 200)
    }}];
}

const BANNED = ['PASTE_', 'TBD', 'PLACEHOLDER', 'INSERT_HERE', 'YOUR_URL'];
const hasPlaceholder = (str) => BANNED.some(p => String(str).includes(p));
if (hasPlaceholder(reply_text) || actions.some(a => hasPlaceholder(JSON.stringify(a)))) {
    reply_text = '[Output validation failed — response contained placeholder values.]';
    actions = [{ type: 'no_op', payload: { reason: 'placeholder_detected' }}];
}

const annotated = actions.map(a => {
    const type = (a && typeof a.type === 'string') ? a.type : 'no_op';
    let risk = riskMap[type] !== undefined ? riskMap[type] : defaultRisk;
    return { type, risk, payload: a.payload || {} };
});

const upstream = $('Assemble prompt').first().json;
return [{ json: {
    reply_text, actions: annotated,
    bot_name: upstream.bot_name, user_chat_id: upstream.user_chat_id,
    task_kind: upstream.task_kind, model_used: $json.model_used || 'unknown',
    parse_error: parseError
}}];"""

BUILD_TASK_CODE = """const src = $input.all()[0];
// ── Loop prevention guard ────────────────────────────────────────────────────
const _incomingBody = (src.json && src.json.body) ? src.json.body : {};
const _attemptCount = (_incomingBody.attempt_count !== undefined)
    ? parseInt(_incomingBody.attempt_count, 10) || 0 : 0;
if (_attemptCount >= 1) {
    return [{ json: { loop_detected: true, original_task: _incomingBody,
        bot_name: 'finance',
        task: 'loop_escalation', user_text: null, chat_id: null, from_agent: 'loop_guard',
        now_iso: new Date().toISOString(), daily_spec: [], weekly_spec: [] } }];
}
// ── End loop prevention ──────────────────────────────────────────────────────

const messageId = (src.json && src.json.message && src.json.message.message_id)
    ? String(src.json.message.message_id) : null;

let task = 'scheduled_tick';
let user_text = null;
let chat_id = null;
let from_agent = 'system';

if (src.json && src.json.message && src.json.message.text) {
    task = 'user_dm';
    user_text = src.json.message.text;
    chat_id = src.json.message.chat && src.json.message.chat.id;
    from_agent = 'telegram';
} else if (src.json && src.json.body && src.json.body.from_agent) {
    task = 'agent_invoke';
    user_text = src.json.body.user_text || src.json.body.objective || null;
    chat_id = src.json.body.chat_id || null;
    from_agent = src.json.body.from_agent;
}

const now = new Date();
const now_iso = now.toISOString();

return [{ json: {
    bot_name: 'finance',
    task,
    user_text,
    chat_id,
    from_agent,
    now_iso,
    message_id: messageId,
    daily_spec: [],
    weekly_spec: [],
} }];"""

EXEC_SQL = """WITH input AS (
  SELECT $1::jsonb AS p
),
memo_insert AS (
  INSERT INTO bridge.agent_memos
    (from_agent, to_agent, memo_type, priority, subject, body_json, related_lead_id)
  SELECT
    $2,
    COALESCE(p->>'to_agent','all'),
    COALESCE(p->>'memo_type','finance_report'),
    COALESCE(p->>'priority','normal'),
    COALESCE(p->>'subject','(no subject)'),
    COALESCE(p->'body_json','{}') || '{"attempt_count": 0}'::jsonb,
    (p->>'related_lead_id')::uuid
  FROM input
  WHERE COALESCE(p->>'memo_type','finance_report') NOT IN ('query','no_op')
  RETURNING memo_id, to_agent, memo_type
)
SELECT
  COALESCE(json_agg(row_to_json(memo_insert)), '[]'::json) AS inserted_memos
FROM memo_insert;"""

# Patch all nodes in the finance bot
for node in finance["nodes"]:
    name = node.get("name", "")
    # Reassign IDs to avoid collision
    node["id"] = str(uuid.uuid4())

    if name == "Assemble prompt":
        node["parameters"]["jsCode"] = ASSEMBLE_CODE
    elif name == "Parse response + risk tag":
        node["parameters"]["jsCode"] = PARSE_CODE
    elif name == "Build task payload":
        node["parameters"]["jsCode"] = BUILD_TASK_CODE
    elif name == "Execute low-risk action":
        if "query" in node.get("parameters", {}):
            node["parameters"]["query"] = EXEC_SQL
    # Patch bot token env var reference
    if "parameters" in node:
        params_str = json.dumps(node["parameters"])
        if "BRIDGE_CEO_BOT_TOKEN" in params_str:
            node["parameters"] = json.loads(
                params_str.replace("BRIDGE_CEO_BOT_TOKEN", "BRIDGE_FINANCE_BOT_TOKEN")
            )
        if "bridge-ceo-" in params_str:
            node["parameters"] = json.loads(
                params_str.replace("bridge-ceo-", "bridge-finance-")
            )

# Update workflow-level name and ID
finance["id"] = str(uuid.uuid4())

out_path = os.path.join(N8N_DIR, "bridge_finance_bot.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(finance, f, ensure_ascii=False, indent=2)

print(f"Created: {out_path}")
print(f"Nodes: {len(finance['nodes'])}")
