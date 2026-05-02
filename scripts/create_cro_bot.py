"""
Create bridge_chief_revenue_optimizer_bot.json — CRO agent.
Monitors revenue signals, identifies upsell/cross-sell, churn risk, pricing experiments.
"""
import json, os, uuid, copy

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N8N_DIR = os.path.join(BASE_DIR, "n8n")

with open(os.path.join(N8N_DIR, "bridge_ceo_bot.json"), encoding="utf-8") as f:
    template = json.load(f)

cro = copy.deepcopy(template)
cro["name"] = "Bridge_ChiefRevenueOptimizer_BOT"
cro["id"] = str(uuid.uuid4())

CRO_SYSTEM = (
    "You are Bridge_ChiefRevenueOptimizer_BOT (CRO), the revenue growth engine of Bridge Digital Solutions. "
    "Your sole obsession is revenue velocity — identifying where money is left on the table and closing that gap.\n\n"
    "ROLE\n"
    "Detect upsell/cross-sell opportunities in existing leads. Identify churn signals before they materialise. "
    "Propose pricing experiments. Track conversion rates per channel. "
    "Report directly to CEO. Coordinate with BizDev on pipeline and with Finance on margin impact.\n\n"
    "REVENUE SIGNALS TO MONITOR (from DB queries):\n"
    "  - bridge.leads WHERE status IN ('proposal_sent','demo_done') AND updated_at < NOW() - INTERVAL '7d' → stale pipeline\n"
    "  - bridge.billing_records WHERE revenue_usd > 0 → paying customers for upsell targeting\n"
    "  - bridge.workflow_events WHERE event_type = 'client_interaction' → engagement signals\n\n"
    "DAILY PRIORITIES:\n"
    "1. Flag leads at risk of going cold (no interaction > 5 days after demo)\n"
    "2. Identify top 3 upsell opportunities in existing paying clients\n"
    "3. Propose one pricing experiment per week (A/B test, bundle, urgency offer)\n"
    "4. Track and report: pipeline value, avg deal size, win rate, churn rate\n\n"
    "REVENUE OKRs (track weekly):\n"
    "  - Pipeline coverage ratio: 3x monthly revenue target\n"
    "  - Lead response time: < 2 hours\n"
    "  - Demo-to-close rate: > 25%\n"
    "  - Monthly churn: < 5%\n\n"
    "ESCALATE TO CEO WHEN:\n"
    "  - A deal > $500 goes cold without follow-up\n"
    "  - Churn rate exceeds 5% in any 30-day window\n"
    "  - A pricing experiment shows > 20% conversion uplift\n"
    "  - A client signals expansion opportunity\n\n"
    "INTERACTION RULES:\n"
    "  - Send revenue signals and recommendations as memos to CEO and COS\n"
    "  - Coordinate with BizDev to qualify new pipeline entries\n"
    "  - Work with Finance to understand margin on proposed deals\n"
    "  - Never directly contact clients — route through PM or COS\n\n"
    "OUTPUT STYLE: Data-driven, concise. Lead with the number, then the action. "
    "Every recommendation must include projected revenue impact and confidence level."
)

GLOBAL_PROTOCOL = """\nconst globalProtocol = `

GLOBAL OPERATING PROTOCOL (non-negotiable):
1. ALWAYS respond in valid JSON only. No prose, no markdown outside JSON strings.
2. EXACT response structure: {"reply_text": "...", "actions": [...]}
3. reply_text: human-readable Telegram message. Brief, role-appropriate.
4. actions: array of {type, payload} objects. Empty array if no actions.
5. NEVER include PLACEHOLDER, PASTE_, TBD, INSERT_HERE values in any field.
6. NEVER mention internal tools (n8n, shell, SQL, workflow IDs) in reply_text.
7. If required data is missing return no_op with status need_input + list exact missing fields.
8. If blocked escalate to Chief of Staff. NEVER retry autonomously.
9. MAX_RETRIES = 1. If attempt_count >= 1 in incoming body_json return no_op immediately.
`;
"""

LOOP_DETECT = """const _buildTask = $('Build task payload').first().json;
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

ASSEMBLE_CODE = LOOP_DETECT + """const task = $('Build task payload').first().json;
const inboxRow = $('Fetch open inbox').first().json || {};
const inbox = Array.isArray(inboxRow.open_inbox) ? inboxRow.open_inbox : [];
const ctxRow = $('Fetch bot context').first().json || {};
const ctx = ctxRow.context ? [ctxRow.context] : [ctxRow];
""" + GLOBAL_PROTOCOL + """
const system = """ + json.dumps(CRO_SYSTEM) + """;

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
      ? `This is a scheduled cadence run. Scan for revenue signals, cold leads, upsell opportunities, and churn risk. Report insights to CEO/COS.`
      : `An inter-agent invocation arrived from '${task.from_agent}': ${task.user_text || 'no message body'}. Decide how to respond.`;

const outputGuard = `\\n\\n[OUTPUT FORMAT]\\nReturn ONLY a JSON object (no prose, no markdown fences):\\n{\\n  "reply_text": "<Telegram message or empty string>",\\n  "actions": [\\n    {"type": "memo",     "payload": {"to_agent": "ceo|chief_of_staff|bizdev|finance", "memo_type": "revenue_signal|upsell_opportunity|churn_risk|pricing_experiment|status", "priority": "urgent|high|normal|low", "subject": "...", "body_json": {...}}},\\n    {"type": "query",    "payload": {"sql": "<safe single SELECT>"}},\\n    {"type": "escalate", "payload": {"subject": "...", "body_json": {...}}},\\n    {"type": "no_op",    "payload": {"reason": "..."}}\\n  ]\\n}\\nKeep actions <= 5.`;

const fullMessage = `${system}${globalProtocol}\\n\\n[CONTEXT]\\n${contextBlock}\\n\\n[TASK]\\n${taskBlock}${outputGuard}`;

return [{ json: {
    message: fullMessage,
    session_id: `bridge-cro-${task.now_iso.slice(0,16).replace(/[:T-]/g,'')}`,
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
        bot_name: 'cro',
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
    bot_name: 'cro',
    task,
    user_text,
    chat_id,
    from_agent,
    now_iso,
    message_id: messageId,
    daily_spec: [],
    weekly_spec: [],
} }];"""

# Patch nodes
for node in cro["nodes"]:
    node["id"] = str(uuid.uuid4())
    name = node.get("name", "")
    params = node.get("parameters", {})

    if name == "Assemble prompt":
        params["jsCode"] = ASSEMBLE_CODE
    elif name == "Parse response + risk tag":
        params["jsCode"] = PARSE_CODE
    elif name == "Build task payload":
        params["jsCode"] = BUILD_TASK_CODE

    # Replace token env var
    params_str = json.dumps(params)
    if "BRIDGE_CEO_BOT_TOKEN" in params_str or "BRIDGE_FINANCE_BOT_TOKEN" in params_str:
        params_str = params_str.replace("BRIDGE_CEO_BOT_TOKEN", "BRIDGE_CHIEF_REVENUE_OPTIMIZER_BOT_TOKEN")
        params_str = params_str.replace("BRIDGE_FINANCE_BOT_TOKEN", "BRIDGE_CHIEF_REVENUE_OPTIMIZER_BOT_TOKEN")
        node["parameters"] = json.loads(params_str)

    # Replace session ID prefixes
    node_str = json.dumps(node)
    if "bridge-ceo-" in node_str or "bridge-finance-" in node_str:
        node_str = node_str.replace("bridge-ceo-", "bridge-cro-").replace("bridge-finance-", "bridge-cro-")
        node.update(json.loads(node_str))

out_path = os.path.join(N8N_DIR, "bridge_chief_revenue_optimizer_bot.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(cro, f, ensure_ascii=False, indent=2)

print(f"Created: {out_path}")
print(f"Nodes: {len(cro['nodes'])}")
print("Bot token env var: BRIDGE_CHIEF_REVENUE_OPTIMIZER_BOT_TOKEN")
