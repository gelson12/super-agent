"""
Bridge OS — Master Architecture Upgrade
Applies Steps 4-7 across all 9 Bridge bot JSON files.
Run once from the repo root: python scripts/patch_bridge_bots.py
"""
import json
import os
import sys

BOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "n8n")

BOTS_ALL = [
    "bridge_security_risk_bot.json",
    "bridge_ceo_bot.json",
    "bridge_chief_of_staff_bot.json",
    "bridge_pm_bot.json",
    "bridge_programmer_bot.json",
    "bridge_researcher_bot.json",
    "bridge_business_development_bot.json",
    "bridge_cleaner_bot.json",
    "bridge_chief_sec_off_bot.json",
]

# ── Step 4: New Parse response + risk tag code ──────────────────────────────
NEW_PARSE_CODE = r"""// ── Pre-formed short-circuit (loop guard) ──────────────────────────────────
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

// ── Standard parse + risk + validation ─────────────────────────────────────
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
    try { const m = raw.match(/\{[\s\S]*\}/); if (m) parsed = JSON.parse(m[0]); } catch (e) {}
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

// ── Placeholder validation ──────────────────────────────────────────────────
const BANNED = ['PASTE_', 'TBD', 'PLACEHOLDER', 'INSERT_HERE', 'YOUR_URL'];
const hasPlaceholder = (str) => BANNED.some(p => String(str).includes(p));
if (hasPlaceholder(reply_text) || actions.some(a => hasPlaceholder(JSON.stringify(a)))) {
    reply_text = '[Output validation failed — response contained placeholder values. Please provide the actual value.]';
    actions = [{ type: 'no_op', payload: { reason: 'placeholder_detected' }}];
}

// ── Internal tool leakage scrubbing (non-blocking) ─────────────────────────
const leakageMap = {
    'n8n workflow creation': 'automation system modification',
    'n8n workflow modification': 'automation system modification',
    'shell command (destructive)': 'system operation',
    'shell command': 'system operation',
    'SQL query': 'data operation',
};
for (const [internal, safe] of Object.entries(leakageMap)) {
    if (reply_text.toLowerCase().includes(internal.toLowerCase()))
        reply_text = reply_text.replace(new RegExp(internal, 'gi'), safe);
}

// ── Risk annotation ─────────────────────────────────────────────────────────
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

NEW_PARSE_CODE_SECURITY = r"""// ── Pre-formed short-circuit (loop guard) ──────────────────────────────────
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

// ── Standard parse + risk + validation ─────────────────────────────────────
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
    try { const m = raw.match(/\{[\s\S]*\}/); if (m) parsed = JSON.parse(m[0]); } catch (e) {}
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

// ── Placeholder validation ──────────────────────────────────────────────────
const BANNED = ['PASTE_', 'TBD', 'PLACEHOLDER', 'INSERT_HERE', 'YOUR_URL'];
const hasPlaceholder = (str) => BANNED.some(p => String(str).includes(p));
if (hasPlaceholder(reply_text) || actions.some(a => hasPlaceholder(JSON.stringify(a)))) {
    reply_text = '[Output validation failed — response contained placeholder values. Please provide the actual value.]';
    actions = [{ type: 'no_op', payload: { reason: 'placeholder_detected' }}];
}

// ── Internal tool leakage scrubbing (non-blocking) ─────────────────────────
const leakageMap = {
    'n8n workflow creation': 'automation system modification',
    'n8n workflow modification': 'automation system modification',
    'shell command (destructive)': 'system operation',
    'shell command': 'system operation',
    'SQL query': 'data operation',
};
for (const [internal, safe] of Object.entries(leakageMap)) {
    if (reply_text.toLowerCase().includes(internal.toLowerCase()))
        reply_text = reply_text.replace(new RegExp(internal, 'gi'), safe);
}

// ── Risk annotation (Security bot: three-tier override) ─────────────────────
const annotated = actions.map(a => {
    const type = (a && typeof a.type === 'string') ? a.type : 'no_op';
    let risk = riskMap[type] !== undefined ? riskMap[type] : defaultRisk;
    if (type === 'escalate' && a.payload) {
        const sl = a.payload.security_level || '';
        if (sl === 'restricted') risk = 'medium';
        if (sl === 'critical')   risk = 'high';
    }
    return { type, risk, payload: a.payload || {} };
});

const upstream = $('Assemble prompt').first().json;
return [{ json: {
    reply_text, actions: annotated,
    bot_name: upstream.bot_name, user_chat_id: upstream.user_chat_id,
    task_kind: upstream.task_kind, model_used: $json.model_used || 'unknown',
    parse_error: parseError
}}];"""

# ── Step 5: Loop guard prefix for Build task payload ───────────────────────
BOT_NAMES = {
    "bridge_security_risk_bot.json": "security",
    "bridge_ceo_bot.json": "ceo",
    "bridge_chief_of_staff_bot.json": "chief_of_staff",
    "bridge_pm_bot.json": "pm",
    "bridge_programmer_bot.json": "programmer",
    "bridge_researcher_bot.json": "researcher",
    "bridge_business_development_bot.json": "bizdev",
    "bridge_cleaner_bot.json": "cleaner",
    "bridge_chief_sec_off_bot.json": "cso",
}

def build_loop_guard(bot_name):
    return f"""// ── Loop prevention guard ────────────────────────────────────────────────────
const _incomingBody = (src.json && src.json.body) ? src.json.body : {{}};
const _attemptCount = (_incomingBody.attempt_count !== undefined)
    ? parseInt(_incomingBody.attempt_count, 10) || 0 : 0;
if (_attemptCount >= 1) {{
    return [{{ json: {{ loop_detected: true, original_task: _incomingBody,
        bot_name: '{bot_name}',
        task: 'loop_escalation', user_text: null, chat_id: null, from_agent: 'loop_guard',
        now_iso: new Date().toISOString(), daily_spec: [], weekly_spec: [] }} }}];
}}
// ── End loop prevention ──────────────────────────────────────────────────────
"""

# ── Step 6: Global Operating Protocol ──────────────────────────────────────
GLOBAL_PROTOCOL = r"""
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

LOOP_DETECTION_PREFIX = r"""const _buildTask = $('Build task payload').first().json;
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

# ── Step 7: Bot-specific system prompt additions ───────────────────────────
SECURITY_PRINCIPLES = """PRINCIPLES: Verify before blocking. Trust internal Bridge agents by default — block only confirmed, specific threats. Speed matters: a false positive halting legitimate work is a risk.

THREE-TIER SECURITY CLASSIFICATION:

TIER 1 — SAFE (auto-approve, no notification required):
  Package installs, deployments to Railway/Vercel, website builds, workflow creation per PM/COS instruction, Telegram notifications, read-only queries, health checks.
  → Emit memo action with memo_type="security_review", status="SAFE"

TIER 2 — RESTRICTED (Telegram approval DM required):
  New third-party API integrations, bulk data exports, credential-touching workflows.
  → Emit escalate action with security_level="restricted"
  Telegram message:
    🚨 ACTION REQUIRES APPROVAL
    Agent: {origin} | Action: {objective}
    Security Level: RESTRICTED
    Summary: {summary}
    Reply: APPROVE to proceed | DENY to cancel

TIER 3 — CRITICAL (passcode required):
  Destructive shell (rm -rf, drop table, force-push), irreversible data deletion.
  → Emit escalate action with security_level="critical", requires_passcode=true
  Telegram message:
    🔴 CRITICAL — PASSCODE REQUIRED
    Reply: APPROVE alphaXXX to proceed"""

COS_ORCHESTRATION = """
ORCHESTRATION AUTHORITY:
When you receive a memo with memo_type='execution_approved' from CEO:
1. Read approved_to_agent field
2. Issue memo to that agent with memo_type='execution_directive', body containing original body_json + status='approved_to_proceed' + attempt_count=0
3. Log handoff in workflow_events memo

LOOP ESCALATION HANDLING:
When you receive a memo with reason='loop_detected':
1. Archive the loop memo
2. Reply to human (reply_text) describing which task looped
3. Recommend manual intervention — do NOT re-dispatch"""

BIZDEV_GATE = """
VALIDATION GATE (mandatory before any proposal to CEO):
body_json.revenue_review MUST contain:
  customer_segments (array), distribution_channels (array), revenue_model (string),
  time_to_first_revenue_weeks (integer), revenue_risks (array),
  confidence ("low"|"medium"|"high"), verdict ("proceed"|"hold"|"reject")
If any field missing → return no_op with status="need_input" listing missing fields.
Never send incomplete proposal to CEO."""

CLEANER_SAFETY = """
SAFETY RULE: Before any cleanup action, set requires_approval=true in payload.
Describe EXACTLY what will be removed and why in reply_text.
Never cleanup >10 records per action. Archive, do not delete, unless explicitly instructed."""

# CEO post-approval SQL CTE addition
CEO_COS_TRIGGER_CTE = """cos_trigger AS (
  INSERT INTO bridge.agent_memos (from_agent, to_agent, memo_type, priority, subject, body_json)
  SELECT 'ceo', 'chief_of_staff', 'execution_approved', 'high',
         'CEO approval granted — begin execution chain',
         jsonb_build_object('approved_memo_id', memo_id, 'approved_to_agent', to_agent,
                            'original_body', (SELECT p FROM input), 'status', 'execution_approved',
                            'attempt_count', 0)
  FROM memo_insert WHERE memo_type = 'approval_granted'
  RETURNING memo_id AS cos_memo_id
),"""

# ── Helpers ─────────────────────────────────────────────────────────────────

def patch_node(nodes, node_name, fn):
    """Apply fn(node) to the first node matching node_name. Returns True if found."""
    for node in nodes:
        if node.get("name") == node_name:
            fn(node)
            return True
    return False


def get_jscode(node):
    return node.get("parameters", {}).get("jsCode", "")


def set_jscode(node, code):
    if "parameters" not in node:
        node["parameters"] = {}
    node["parameters"]["jsCode"] = code


def get_sql(node):
    return node.get("parameters", {}).get("query", "")


def set_sql(node, sql):
    if "parameters" not in node:
        node["parameters"] = {}
    node["parameters"]["query"] = sql


def patch_bot(filename):
    path = os.path.join(BOT_DIR, filename)
    if not os.path.exists(path):
        print(f"  SKIP (not found): {filename}")
        return

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    nodes = data.get("nodes", [])
    bot_key = BOT_NAMES.get(filename, "unknown")
    changes = []

    # ── Step 4: Replace Parse response + risk tag ──────────────────────────
    parse_code = NEW_PARSE_CODE_SECURITY if filename == "bridge_security_risk_bot.json" else NEW_PARSE_CODE

    def replace_parse(node):
        set_jscode(node, parse_code)
        changes.append("Parse response replaced")

    if not patch_node(nodes, "Parse response + risk tag", replace_parse):
        print(f"  WARNING: 'Parse response + risk tag' node not found in {filename}")

    # ── Step 5: Prepend loop guard to Build task payload ───────────────────
    def prepend_loop_guard(node):
        code = get_jscode(node)
        anchor = "const src = $input.all()[0];"
        if anchor in code and "loop_detected" not in code:
            guard = build_loop_guard(bot_key)
            code = code.replace(anchor, anchor + "\n" + guard, 1)
            set_jscode(node, code)
            changes.append("Build task payload loop guard added")
        elif "loop_detected" in code:
            changes.append("Build task payload loop guard already present")
        else:
            print(f"  WARNING: anchor not found in Build task payload for {filename}")

    if not patch_node(nodes, "Build task payload", prepend_loop_guard):
        print(f"  WARNING: 'Build task payload' node not found in {filename}")

    # ── Step 6: Assemble prompt — loop detection + globalProtocol ──────────
    def patch_assemble(node):
        code = get_jscode(node)

        # 6a: Prepend loop detection (only if not already added)
        if "_buildTask.loop_detected" not in code:
            code = LOOP_DETECTION_PREFIX + code
            changes.append("Assemble prompt loop detection prepended")
        else:
            changes.append("Assemble prompt loop detection already present")

        # 6b: Add globalProtocol variable (only if not already added)
        if "globalProtocol" not in code:
            # Insert after the loop detection prefix (after the first blank line after LOOP_DETECTION_PREFIX)
            # Actually insert before "const system = "
            code = code.replace("const system = ", GLOBAL_PROTOCOL + "\nconst system = ", 1)
            changes.append("globalProtocol variable added")

        # 6c: Inject ${globalProtocol} into fullMessage
        if "${globalProtocol}" not in code:
            # fullMessage = `${system}\n\n[CONTEXT] → inject after ${system}
            code = code.replace(
                "${system}\\n\\n[CONTEXT]",
                "${system}${globalProtocol}\\n\\n[CONTEXT]",
                1
            )
            if "${globalProtocol}" not in code:
                # Try without escape (in-memory string)
                code = code.replace(
                    "${system}\n\n[CONTEXT]",
                    "${system}${globalProtocol}\n\n[CONTEXT]",
                    1
                )
            if "${globalProtocol}" in code:
                changes.append("globalProtocol injected into fullMessage")
            else:
                print(f"  WARNING: could not inject globalProtocol into fullMessage for {filename}")

        set_jscode(node, code)

    if not patch_node(nodes, "Assemble prompt", patch_assemble):
        print(f"  WARNING: 'Assemble prompt' node not found in {filename}")

    # ── Step 7: Bot-specific system prompt additions ───────────────────────

    def append_to_system(node, text):
        """Append text to the const system = "..." string in jsCode."""
        code = get_jscode(node)
        # System string ends with "; (closing double-quote then semicolon)
        # Pattern: `const system = "...";` — find the closing `";` and insert before it
        # We'll find the last `";` in the system declaration block
        idx = code.find('const system = "')
        if idx == -1:
            print(f"  WARNING: const system not found in Assemble prompt for {filename}")
            return False
        # Find the closing "; after the system string
        end_marker = '";\n'
        end_idx = code.find(end_marker, idx)
        if end_idx == -1:
            end_marker = '";\r\n'
            end_idx = code.find(end_marker, idx)
        if end_idx == -1:
            print(f"  WARNING: could not find system string end in {filename}")
            return False
        # Insert the text before the closing ";
        code = code[:end_idx] + text + code[end_idx:]
        set_jscode(node, code)
        return True

    if filename == "bridge_security_risk_bot.json":
        def patch_security_system(node):
            code = get_jscode(node)
            if "THREE-TIER SECURITY CLASSIFICATION" not in code:
                # Find "PRINCIPLES" section and replace it
                # The existing PRINCIPLES section ends before WHAT YOU REVIEW or similar
                if "PRINCIPLES" in code:
                    # Find PRINCIPLES block and replace up to next section header
                    import re as _re
                    # Replace old PRINCIPLES section
                    code = _re.sub(
                        r'PRINCIPLES[^\n]*\n(?:.*\n)*?(?=WHAT YOU REVIEW|WHAT YOU MUST)',
                        SECURITY_PRINCIPLES + "\n\n",
                        code, count=1
                    )
                    set_jscode(node, code)
                    changes.append("Security three-tier classification added")
                else:
                    # Append to system string
                    if append_to_system(node, "\n\n" + SECURITY_PRINCIPLES):
                        changes.append("Security three-tier classification appended")
            else:
                changes.append("Security three-tier already present")
        patch_node(nodes, "Assemble prompt", patch_security_system)

    elif filename == "bridge_chief_of_staff_bot.json":
        def patch_cos_system(node):
            code = get_jscode(node)
            if "ORCHESTRATION AUTHORITY" not in code:
                if append_to_system(node, COS_ORCHESTRATION):
                    changes.append("COS orchestration authority added")
            else:
                changes.append("COS orchestration authority already present")
        patch_node(nodes, "Assemble prompt", patch_cos_system)

    elif filename == "bridge_business_development_bot.json":
        def patch_bizdev_system(node):
            code = get_jscode(node)
            if "VALIDATION GATE" not in code:
                if append_to_system(node, BIZDEV_GATE):
                    changes.append("BizDev validation gate added")
            else:
                changes.append("BizDev validation gate already present")
        patch_node(nodes, "Assemble prompt", patch_bizdev_system)

    elif filename == "bridge_cleaner_bot.json":
        def patch_cleaner_system(node):
            code = get_jscode(node)
            if "SAFETY RULE" not in code:
                if append_to_system(node, CLEANER_SAFETY):
                    changes.append("Cleaner safety rule added")
            else:
                changes.append("Cleaner safety rule already present")
        patch_node(nodes, "Assemble prompt", patch_cleaner_system)

    # ── Step 7B: CEO post-approval COS trigger SQL ─────────────────────────
    if filename == "bridge_ceo_bot.json":
        def patch_ceo_sql(node):
            sql = get_sql(node)
            if "cos_trigger" not in sql and "memo_insert" in sql:
                # Find memo_insert CTE closing and insert cos_trigger after
                # The memo_insert CTE ends with "RETURNING memo_id" or similar
                insert_after = "RETURNING memo_id"
                if insert_after in sql:
                    idx = sql.find(insert_after)
                    # Find the end of this CTE (the closing '),' or ') ,')
                    cte_end = sql.find("\n)", idx)
                    if cte_end != -1:
                        # Insert cos_trigger CTE after memo_insert
                        sql = sql[:cte_end + 2] + ",\n" + CEO_COS_TRIGGER_CTE + sql[cte_end + 2:]
                        # Add cos_triggered to final SELECT
                        final_select_marker = "SELECT memo_id"
                        if final_select_marker in sql:
                            sql = sql.replace(
                                final_select_marker,
                                final_select_marker + ",\n       (SELECT cos_memo_id FROM cos_trigger) AS cos_triggered",
                                1
                            )
                        set_sql(node, sql)
                        changes.append("CEO post-approval COS trigger SQL added")
                else:
                    print(f"  WARNING: could not locate CTE anchor in CEO Execute low-risk SQL")
            elif "cos_trigger" in sql:
                changes.append("CEO COS trigger SQL already present")

        if not patch_node(nodes, "Execute low-risk action", patch_ceo_sql):
            print(f"  WARNING: 'Execute low-risk action' node not found in CEO bot")

    # ── Step 7G: All bots — add attempt_count=0 to Execute low-risk SQL ────
    def patch_exec_sql_attempt_count(node):
        sql = get_sql(node)
        if sql and "attempt_count" not in sql and "COALESCE(p->'body_json'" in sql:
            sql = sql.replace(
                "COALESCE(p->'body_json','{}'::jsonb)",
                "COALESCE(p->'body_json','{}'::jsonb) || '{\"attempt_count\": 0}'::jsonb",
                1
            )
            set_sql(node, sql)
            changes.append("Execute low-risk SQL attempt_count=0 added")

    patch_node(nodes, "Execute low-risk action", patch_exec_sql_attempt_count)

    # ── Save ───────────────────────────────────────────────────────────────
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  OK {filename}: {', '.join(changes)}")


# ── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Bridge bot patcher — BOT_DIR: {BOT_DIR}\n")
    errors = 0
    for bot in BOTS_ALL:
        try:
            patch_bot(bot)
        except Exception as e:
            print(f"  ERROR {bot}: {e}")
            import traceback; traceback.print_exc()
            errors += 1
    print(f"\nDone. {len(BOTS_ALL) - errors}/{len(BOTS_ALL)} bots patched.")
    sys.exit(errors)
