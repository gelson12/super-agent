"""
Bridge OS — Intelligence Layer Upgrade Script
=============================================
Applies Steps 2-5 from the Intelligence Layer Upgrade plan:

  Step 2: CRO bot — full gating system prompt + memory/perf integration
  Step 3: Routing chain enforcement (BizDev→CRO, CEO CRO gate, COS enforcement)
           + all-bot performance self-reporting + auto-block memory check
  Step 4: Create bridge_performance_tracker.json (08:30 UTC daily)
  Step 5: Create bridge_meta_intelligence.json (23:30 UTC daily)

Run: python scripts/upgrade_intelligence_layer.py
"""
import copy
import json
import os
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N8N_DIR = os.path.join(BASE_DIR, "n8n")

# ── Helpers ────────────────────────────────────────────────────────────────────

def load(filename):
    with open(os.path.join(N8N_DIR, filename), encoding="utf-8") as f:
        return json.load(f)

def save(data, filename):
    path = os.path.join(N8N_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {filename}")

def find_node(bot, name):
    for n in bot["nodes"]:
        if n.get("name") == name:
            return n
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — CRO BOT: FULL GATING SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

CRO_SYSTEM_FULL = (
    "You are Bridge_ChiefRevenueOptimizer_BOT (CRO), the revenue intelligence engine and "
    "commercial gatekeeper of Bridge Digital Solutions.\n\n"

    "WORKFLOW POSITION (mandatory enforcement):\n"
    "  BizDev → [CRO GATE] → Security → CEO\n"
    "  Every project proposal with revenue > $0 MUST pass CRO scoring before reaching Security or CEO.\n"
    "  Score < 50  → REJECT: send memo type 'cro_rejected' to bizdev, log to project_memory\n"
    "  Score 50-69 → REVISE: send memo type 'cro_revision_needed' to bizdev with specific improvements required\n"
    "  Score ≥ 70  → APPROVE: send memo type 'cro_approved' to chief_sec_off for security clearance\n\n"

    "GATE MODE — triggered by memo_type='cro_review_request':\n"
    "1. Extract project_name from body_json. Query historical memory via the context block.\n"
    "2. If historical_context shows auto_block=true (rejected ≥ 2x) → immediately REJECT with reason='auto_block_repeated_rejection'.\n"
    "3. If similar past success found → boost score by up to +10 pts (document this in historical_comparison).\n"
    "4. If similar past failure found → reduce score by up to -15 pts (document the lesson applied).\n"
    "5. Score the proposal using the SCORING RUBRIC below (0-100).\n"
    "6. Reply with memo_type='cro_review_complete' containing the full CRO Revenue Optimization Report.\n"
    "7. Also send memo_type='cro_evaluation_record' to chief_of_staff for audit trail.\n\n"

    "SCORING RUBRIC (total 100 pts):\n"
    "  Revenue model clarity:          0-20 pts\n"
    "  Market size & timing:           0-15 pts\n"
    "  Pricing competitiveness:        0-15 pts\n"
    "  Funnel conversion realism:      0-15 pts\n"
    "  Historical success alignment:   0-15 pts (from memory context)\n"
    "  Risk-adjusted margin:           0-20 pts\n\n"

    "REQUIRED OUTPUT for cro_review_complete memo body_json:\n"
    "{\n"
    "  \"project_name\": \"...\",\n"
    "  \"cro_score\": 0-100,\n"
    "  \"revenue_model\": \"...\",\n"
    "  \"estimated_monthly_revenue_usd\": 0,\n"
    "  \"growth_potential\": \"low|medium|high|exponential\",\n"
    "  \"revenue_risks\": [...],\n"
    "  \"optimization_suggestions\": {\n"
    "    \"pricing_strategy\": \"...\",\n"
    "    \"upsells\": \"...\",\n"
    "    \"funnel_improvements\": \"...\",\n"
    "    \"automation_opportunities\": \"...\"\n"
    "  },\n"
    "  \"historical_comparison\": {\n"
    "    \"similar_projects\": [...],\n"
    "    \"outcome\": \"...\",\n"
    "    \"lessons_applied\": \"...\"\n"
    "  },\n"
    "  \"recommendation\": \"APPROVE|REVISE|REJECT\",\n"
    "  \"next_action\": \"...\"\n"
    "}\n\n"

    "MONITORING MODE — triggered by scheduled_tick:\n"
    "  - Flag leads at risk of going cold (no interaction > 5 days after demo)\n"
    "  - Identify top 3 upsell opportunities in existing paying clients\n"
    "  - Propose one pricing experiment per week (A/B test, bundle, urgency offer)\n"
    "  - Track and report: pipeline value, avg deal size, win rate, churn rate\n\n"

    "REVENUE OKRs (track weekly):\n"
    "  - Pipeline coverage ratio: 3x monthly revenue target\n"
    "  - Lead response time: < 2 hours\n"
    "  - Demo-to-close rate: > 25%\n"
    "  - Monthly churn: < 5%\n\n"

    "REVENUE SIGNALS TO MONITOR (from DB queries):\n"
    "  - bridge.leads WHERE status IN ('proposal_sent','demo_done') AND updated_at < NOW() - INTERVAL '7d'\n"
    "  - bridge.billing_records WHERE revenue_usd > 0\n"
    "  - bridge.workflow_events WHERE event_type = 'client_interaction'\n\n"

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

# Auto-block + memory context injection for CRO Assemble prompt
CRO_MEMORY_BLOCK = """// ── CRO: Query project memory for gating decisions ──────────────────────────
let _historicalContext = { similar_projects: [], rejection_count: 0, auto_block: false, semantic_matches: [] };
const _isCroReview = task.task === 'agent_invoke' && (task.user_text || '').includes('cro_review_request');
const _projectNameForQuery = (() => {
    try {
        const _body = JSON.parse(task.user_text || '{}');
        return _body.project_name || _body.subject || task.user_text || '';
    } catch(e) { return task.user_text || ''; }
})();
if (_projectNameForQuery && _projectNameForQuery.length > 5) {
    try {
        const _memResp = await $http.post(
            `${$env.SUPER_AGENT_URL || 'https://super-agent-production.up.railway.app'}/webhook/memory-query`,
            { project_name: _projectNameForQuery, api_key: $env.N8N_API_KEY || '' }
        );
        if (_memResp && _memResp.data) {
            _historicalContext = _memResp.data;
        }
        if (_historicalContext.auto_block === true && _isCroReview) {
            return [{ json: {
                message: JSON.stringify({
                    reply_text: `🚫 AUTO-BLOCK: Project "${_projectNameForQuery}" has been rejected ${_historicalContext.rejection_count} time(s) previously. CRO auto-blocking. CEO override required.`,
                    actions: [{ type: 'memo', payload: {
                        to_agent: 'bizdev', memo_type: 'cro_rejected',
                        priority: 'high',
                        subject: `CRO AUTO-BLOCK: ${_projectNameForQuery}`,
                        body_json: {
                            project_name: _projectNameForQuery,
                            cro_score: 0, recommendation: 'REJECT',
                            reason: 'auto_block_repeated_rejection',
                            rejection_count: _historicalContext.rejection_count,
                            next_action: 'Requires CEO override to proceed. Do not resubmit without fundamentally different value proposition.',
                            attempt_count: 0
                        }
                    }}]
                }),
                session_id: `bridge-cro-${Date.now()}`,
                task_kind: 'auto_block', user_chat_id: task.chat_id,
                bot_name: task.bot_name, _pre_formed: true
            }}];
        }
    } catch(e) { /* non-blocking — memory query failure does not stop CRO execution */ }
}

"""

def build_cro_assemble_code(original_code):
    """Insert memory block after loop detection, before context assembly."""
    # Find insertion point: after the loop_detected block
    marker = "\nconst task = $('Build task payload').first().json;"
    idx = original_code.find(marker)
    if idx == -1:
        return original_code  # fallback: don't break it
    insert_at = idx + len(marker)
    # Build new code: loop detection + task + memory block + rest
    before = original_code[:insert_at]
    after = original_code[insert_at:]
    # Inject memory block + historical context into the context block
    new_code = before + "\n" + CRO_MEMORY_BLOCK + after
    # Also inject historical context into the contextBlock JSON
    ctx_marker = "    bot_context: ctx,"
    if ctx_marker in new_code:
        new_code = new_code.replace(
            ctx_marker,
            ctx_marker + "\n    historical_context: _historicalContext,"
        )
    return new_code

# Performance self-reporting CTE to append to every bot's Execute SQL
PERF_UPDATE_CTE = """,
perf_self_report AS (
  INSERT INTO bridge.agent_performance
    (agent_name, date, tasks_total, tasks_success)
  VALUES ($2, CURRENT_DATE, 1, 1)
  ON CONFLICT (agent_name, date) DO UPDATE SET
    tasks_total   = bridge.agent_performance.tasks_total + 1,
    tasks_success = bridge.agent_performance.tasks_success + 1
  RETURNING agent_name
)"""

# CRO evaluation INSERT CTE (CRO bot only)
CRO_EVAL_CTE = """,
cro_eval_log AS (
  INSERT INTO bridge.cro_evaluations
    (project_name, from_agent, cro_score, revenue_model, estimated_monthly_revenue_usd,
     growth_potential, revenue_risks, optimization_suggestions, historical_comparison,
     recommendation, next_action)
  SELECT
    COALESCE((p->>'project_name'), ''),
    $2,
    (p->>'cro_score')::integer,
    COALESCE(p->>'revenue_model', ''),
    (p->>'estimated_monthly_revenue_usd')::numeric,
    COALESCE(p->>'growth_potential', ''),
    COALESCE(p->'revenue_risks', '[]'::jsonb),
    COALESCE(p->'optimization_suggestions', '{}'::jsonb),
    COALESCE(p->'historical_comparison', '{}'::jsonb),
    COALESCE(p->>'recommendation', ''),
    COALESCE(p->>'next_action', '')
  FROM input
  WHERE $3 = 'memo' AND COALESCE(p->>'memo_type','') IN ('cro_review_complete','cro_rejected','cro_revision_needed')
  RETURNING id
)"""

def patch_execute_sql(query, bot_name, is_cro=False):
    """Append performance self-report CTE (and CRO eval CTE for CRO bot) to Execute SQL."""
    # Find the final SELECT statement
    final_select = "\nSELECT\n"
    idx = query.rfind(final_select)
    if idx == -1:
        return query  # don't break unknown format
    insert_at = idx

    addition = PERF_UPDATE_CTE
    if is_cro:
        addition = CRO_EVAL_CTE + PERF_UPDATE_CTE

    new_query = query[:insert_at] + addition + query[insert_at:]
    # Also update the SELECT to include new CTE results
    new_query = new_query.replace(
        "  (SELECT event_id FROM event_log)   AS event_logged;",
        "  (SELECT event_id FROM event_log)   AS event_logged,\n"
        "  (SELECT agent_name FROM perf_self_report) AS perf_recorded;"
    )
    return new_query

# Auto-block check for all non-CRO bots (lighter version: just check, no block action)
ALL_BOT_AUTOBLOCK = """// ── Memory auto-block check (non-blocking) ──────────────────────────────────
let _autoBlockActive = false;
const _querySubject = task.user_text || task.subject || '';
if (_querySubject && _querySubject.length > 5) {
    try {
        const _memCheck = await $http.post(
            `${$env.SUPER_AGENT_URL || 'https://super-agent-production.up.railway.app'}/webhook/memory-query`,
            { project_name: _querySubject.slice(0, 100), api_key: $env.N8N_API_KEY || '' }
        );
        if (_memCheck && _memCheck.data && _memCheck.data.auto_block === true) {
            _autoBlockActive = true;
            return [{ json: {
                message: JSON.stringify({
                    reply_text: `🚫 AUTO-BLOCK: Project has been rejected ${_memCheck.data.rejection_count} time(s). Route to CRO then CEO for override.`,
                    actions: [{ type: 'no_op', payload: {
                        reason: 'auto_block_repeated_rejection',
                        rejection_count: _memCheck.data.rejection_count,
                        similar_projects: _memCheck.data.similar_projects
                    }}]
                }),
                session_id: `bridge-auto-block-${Date.now()}`,
                task_kind: 'auto_block', user_chat_id: task.chat_id,
                bot_name: task.bot_name, _pre_formed: true
            }}];
        }
    } catch(e) { /* non-blocking */ }
}

"""

def patch_assemble_prompt_autoblock(code, is_cro=False):
    """Inject auto-block check after task assignment, before context assembly."""
    marker = "\nconst task = $('Build task payload').first().json;"
    idx = code.find(marker)
    if idx == -1:
        return code
    insert_at = idx + len(marker)
    block = CRO_MEMORY_BLOCK if is_cro else ALL_BOT_AUTOBLOCK
    return code[:insert_at] + "\n" + block + code[insert_at:]

# ── System prompt additions ────────────────────────────────────────────────────

BIZDEV_ROUTING_ADDITION = (
    "\n\nPROPOSAL ROUTING TO CRO (MANDATORY — enforced before CEO):\n"
    "When verdict=approve OR verdict=needs_iteration:\n"
    "  → Send memo with memo_type='cro_review_request' to_agent='cro' (NOT directly to CEO)\n"
    "  → Include all revenue_review fields in body_json plus: project_name, customer_segments,\n"
    "     distribution_channels, revenue_model, time_to_first_revenue_weeks, revenue_risks,\n"
    "     confidence, estimated_monthly_revenue_usd\n"
    "  → Set attempt_count=0 in body_json\n"
    "  → Wait for cro_review_complete memo before CEO sees the project\n\n"
    "NEVER send a proposal directly to CEO. CRO MUST validate first.\n"
    "If you receive cro_revision_needed memo: address the specific improvements requested,\n"
    "then resubmit to CRO with updated figures. Document what changed in body_json.revision_notes."
)

CEO_CRO_GATE_ADDITION = (
    "\n\nCRO GATE (MANDATORY — enforced on all project approvals):\n"
    "When receiving any project proposal or approval_request:\n"
    "  1. Check body_json.cro_score — MUST be present and >= 70\n"
    "  2. Check body_json.recommendation — MUST be 'APPROVE'\n"
    "  3. If cro_score missing or < 70 → route memo_type='cro_review_request' to CRO, do NOT approve\n"
    "  4. If cro_score >= 70 → proceed with normal approval flow\n\n"
    "FINAL PRIORITY SCORE = Base_PM_Score * 0.40 + cro_score * 0.40 + historical_confidence * 0.20\n\n"
    "PERFORMANCE AWARENESS:\n"
    "  Daily check /webhook/performance-dashboard for agent success rates.\n"
    "  If any agent success_rate < 60% for 3 consecutive days → include in strategic memo to COS.\n"
    "  If authority_level < 3 for any agent → require COS co-approval for that agent's decisions."
)

COS_CHAIN_ADDITION = (
    "\n\nAPPROVAL CHAIN ENFORCEMENT (mandatory routing order):\n"
    "  REQUIRED ORDER: BizDev → CRO → Security (CSO) → CEO\n\n"
    "  If a memo arrives at CEO or Security without cro_score in body_json:\n"
    "    → Route to CRO first with memo_type='cro_review_request'\n"
    "    → Do NOT forward to Security or CEO until CRO review is complete\n\n"
    "  On receiving cro_review_complete:\n"
    "    score ≥ 70  → forward to chief_sec_off for security clearance\n"
    "    score 50-69 → route back to bizdev with revision notes\n"
    "    score < 50  → route back to bizdev as rejected (do not escalate)\n\n"
    "PERFORMANCE MONITORING:\n"
    "  Daily: check /webhook/performance-dashboard for agent health.\n"
    "  If any agent success_rate < 60% → flag to CEO in daily COS report.\n"
    "  If any agent authority_level < 3 → require COS co-approval for that agent's actions.\n"
    "  Authority adjustments happen automatically via the Performance Tracker workflow at 08:30 UTC."
)

# ── Bots to process ────────────────────────────────────────────────────────────
# (filename, bot_key, is_cro, add_bizdev_routing, add_ceo_gate, add_cos_chain)
BOTS = [
    ("bridge_chief_revenue_optimizer_bot.json", "cro",           True,  False, False, False),
    ("bridge_ceo_bot.json",                     "ceo",           False, False, True,  False),
    ("bridge_chief_of_staff_bot.json",          "chief_of_staff",False, False, False, True),
    ("bridge_business_development_bot.json",    "bizdev",        False, True,  False, False),
    ("bridge_pm_bot.json",                      "pm",            False, False, False, False),
    ("bridge_programmer_bot.json",              "programmer",    False, False, False, False),
    ("bridge_researcher_bot.json",              "researcher",    False, False, False, False),
    ("bridge_cleaner_bot.json",                 "cleaner",       False, False, False, False),
    ("bridge_chief_sec_off_bot.json",           "cso",           False, False, False, False),
    ("bridge_finance_bot.json",                 "finance",       False, False, False, False),
]

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — PERFORMANCE TRACKER WORKFLOW (08:30 UTC daily)
# ═══════════════════════════════════════════════════════════════════════════════

PERF_TRACKER_DASHBOARD_CODE = """const data = $('Pull performance data').first().json;
const agents = (data && data.agents) ? data.agents : [];
const croRows = $('Pull CRO evaluations').first().json || {};
const croList = Array.isArray(croRows) ? croRows : (croRows.rows || []);

const today = new Date().toISOString().slice(0, 10);
let msg = `📊 AGENT PERFORMANCE DASHBOARD — ${today}\\n\\n`;

const approved = croList.filter(r => r.recommendation === 'APPROVE').length;
const revised  = croList.filter(r => r.recommendation === 'REVISE').length;
const rejected = croList.filter(r => r.recommendation === 'REJECT').length;
const avgScore = croList.length > 0
    ? Math.round(croList.reduce((s,r) => s + (r.cro_score || 0), 0) / croList.length)
    : null;

msg += `🏆 CRO GATE TODAY: ${approved} approved | ${revised} revised | ${rejected} rejected`;
if (avgScore !== null) msg += ` | avg score: ${avgScore}/100`;
msg += '\\n\\n';

msg += `👥 AGENT SCORES:\\n`;
for (const a of agents) {
    const rate = a.success_rate !== null ? `${a.success_rate}%` : 'n/a';
    const flag = (a.success_rate !== null && a.success_rate < 60) ? ' ⚠️' : '';
    const auth = `[auth:${a.authority}]`;
    msg += `  ${a.agent}: ${a.tasks} tasks, ${rate} success ${auth}${flag}\\n`;
}

const underperf = agents.filter(a => a.success_rate !== null && a.success_rate < 60);
if (underperf.length > 0) {
    msg += `\\n⚠️ UNDERPERFORMING: ${underperf.map(a => a.agent).join(', ')}`;
    msg += ' — authority reduction pending\\n';
}

if (data.best_agent) msg += `\\n🥇 Best today: ${data.best_agent}\\n`;

return [{ json: { telegram_message: msg, agents, underperforming: underperf.map(a => a.agent) }}];"""

PERF_TRACKER_AUTHORITY_CODE = """const data = $('Build dashboard').first().json;
const underperforming = data.underperforming || [];
const agents = data.agents || [];

// Demote agents with < 50% success rate; promote agents with 0 failures (if tasks > 5)
const adjustments = [];
for (const a of agents) {
    if (a.tasks >= 3 && a.success_rate !== null && a.success_rate < 50) {
        adjustments.push({ agent: a.agent, delta: -1, reason: 'success_rate < 50%' });
    } else if (a.tasks >= 5 && a.failed === 0 && a.authority < 10) {
        adjustments.push({ agent: a.agent, delta: +1, reason: 'zero failures today (5+ tasks)' });
    }
}
return [{ json: { adjustments, count: adjustments.length }}];"""

PERF_TRACKER_APPLY_SQL = """WITH adj AS (
  SELECT
    unnest($1::text[]) AS agent_name,
    unnest($2::integer[]) AS delta
)
UPDATE bridge.agent_performance ap
SET authority_level = GREATEST(1, LEAST(10, ap.authority_level + adj.delta))
FROM adj
WHERE ap.agent_name = adj.agent_name
  AND ap.date = CURRENT_DATE
RETURNING ap.agent_name, ap.authority_level;"""


def build_performance_tracker(ceo_chat_id_expr="$env.CEO_TELEGRAM_CHAT_ID",
                               cos_chat_id_expr="$env.COS_TELEGRAM_CHAT_ID",
                               tg_token_expr="$env.BRIDGE_CEO_BOT_TOKEN"):
    """Build the 8-node Performance Tracker workflow JSON."""
    wf_id = str(uuid.uuid4())

    def node(name, ntype, position, params, creds=None):
        n = {"id": str(uuid.uuid4()), "name": name, "type": ntype,
             "typeVersion": 1, "position": position, "parameters": params}
        if creds:
            n["credentials"] = creds
        return n

    # Node positions (evenly spaced horizontal layout)
    nodes = [
        node("Schedule: 08:30 UTC", "n8n-nodes-base.scheduleTrigger", [240, 300], {
            "rule": {"interval": [{"field": "hours", "minutesInterval": 1}]},
            "cronExpression": "30 8 * * *"
        }),
        node("Pull performance data", "n8n-nodes-base.httpRequest", [480, 300], {
            "method": "GET",
            "url": "={{ $env.SUPER_AGENT_URL || 'https://super-agent-production.up.railway.app' }}/webhook/performance-dashboard",
            "responseFormat": "json"
        }),
        node("Pull CRO evaluations", "n8n-nodes-base.postgres", [720, 300], {
            "operation": "executeQuery",
            "query": "SELECT project_name, cro_score, recommendation, created_at FROM bridge.cro_evaluations WHERE created_at >= NOW() - INTERVAL '24h' ORDER BY cro_score DESC"
        }),
        node("Pull project memory", "n8n-nodes-base.postgres", [720, 480], {
            "operation": "executeQuery",
            "query": "SELECT project_name, outcome, cro_score, actual_revenue_usd FROM bridge.project_memory WHERE updated_at >= NOW() - INTERVAL '7d' ORDER BY updated_at DESC LIMIT 10"
        }),
        node("Build dashboard", "n8n-nodes-base.code", [960, 300], {
            "jsCode": PERF_TRACKER_DASHBOARD_CODE
        }),
        node("Authority adjustment", "n8n-nodes-base.code", [1200, 300], {
            "jsCode": PERF_TRACKER_AUTHORITY_CODE
        }),
        node("Apply authority changes", "n8n-nodes-base.postgres", [1440, 300], {
            "operation": "executeQuery",
            "query": PERF_TRACKER_APPLY_SQL,
            "options": {
                "queryReplacement": (
                    "={{ $json.adjustments.map(a=>a.agent_name) }},"
                    "={{ $json.adjustments.map(a=>a.delta) }}"
                )
            }
        }),
        node("Send to CEO", "n8n-nodes-base.httpRequest", [1680, 240], {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.BRIDGE_CEO_BOT_TOKEN }}/sendMessage",
            "sendBody": True,
            "bodyParameters": {
                "parameters": [
                    {"name": "chat_id", "value": f"={ceo_chat_id_expr}"},
                    {"name": "text", "value": "={{ $('Build dashboard').first().json.telegram_message }}"},
                    {"name": "parse_mode", "value": "Markdown"}
                ]
            }
        }),
    ]

    connections = {
        "Schedule: 08:30 UTC": {"main": [[{"node": "Pull performance data", "type": "main", "index": 0}]]},
        "Pull performance data": {"main": [[
            {"node": "Pull CRO evaluations", "type": "main", "index": 0},
            {"node": "Pull project memory", "type": "main", "index": 0}
        ]]},
        "Pull CRO evaluations": {"main": [[{"node": "Build dashboard", "type": "main", "index": 0}]]},
        "Build dashboard": {"main": [[{"node": "Authority adjustment", "type": "main", "index": 0}]]},
        "Authority adjustment": {"main": [[{"node": "Apply authority changes", "type": "main", "index": 0}]]},
        "Apply authority changes": {"main": [[{"node": "Send to CEO", "type": "main", "index": 0}]]},
    }

    return {
        "id": wf_id, "name": "Bridge_Performance_Tracker",
        "active": True, "nodes": nodes, "connections": connections,
        "settings": {"executionOrder": "v1"}
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — META-INTELLIGENCE WORKFLOW (23:30 UTC daily)
# ═══════════════════════════════════════════════════════════════════════════════

META_INTEL_BUILD_CODE = """const row = $input.all()[0].json || {};
const stats = row;
const today = new Date().toISOString().slice(0, 10);

const totalMemos    = stats.total_memos || 0;
const croEvals      = stats.cro_evaluations || 0;
const avgCroScore   = stats.avg_cro_score ? parseFloat(stats.avg_cro_score).toFixed(1) : 'n/a';
const projApproved  = stats.projects_approved || 0;
const projRejected  = stats.projects_rejected || 0;
const revToday      = stats.revenue_today ? `$${parseFloat(stats.revenue_today).toFixed(2)}` : '$0.00';
const bestAgent     = stats.best_agent || 'n/a';
const worstAgent    = stats.worst_agent || 'n/a';

const prompt = `You are the Bridge OS meta-intelligence system. Analyze today's operational data and provide a concise, actionable intelligence report.

TODAY'S DATA (${today}):
- Total inter-agent memos: ${totalMemos}
- CRO evaluations: ${croEvals} (avg score: ${avgCroScore}/100)
- Projects approved: ${projApproved} | rejected: ${projRejected}
- Revenue attributed: ${revToday}
- Best performing agent: ${bestAgent}
- Underperforming agent: ${worstAgent}

Respond with a JSON object:
{
  "system_health": "excellent|good|fair|poor",
  "learning_rate_trend": "improving|stable|declining",
  "revenue_efficiency": "high|medium|low",
  "pattern_alerts": ["...list of detected repeating mistakes or missed opportunities..."],
  "adaptation_recommendations": ["...list of specific changes for tomorrow..."],
  "ai_insight": "...2-3 sentence strategic insight for the CEO..."
}`;

return [{ json: { prompt, today, totalMemos, croEvals, avgCroScore, projApproved, projRejected, revToday, bestAgent, worstAgent }}];"""

META_INTEL_REPORT_CODE = """const ctx = $('Build AI prompt').first().json;
let aiInsight = { system_health: 'unknown', learning_rate_trend: 'stable',
                   revenue_efficiency: 'medium', pattern_alerts: [],
                   adaptation_recommendations: [], ai_insight: '' };

try {
    const raw = $json.response || $json.message || '';
    const cleaned = raw.trim().replace(/^```(?:json)?/, '').replace(/```$/, '').trim();
    const m = cleaned.match(/\\{[\\s\\S]*\\}/);
    if (m) aiInsight = JSON.parse(m[0]);
} catch(e) {}

const today = ctx.today;
let report = `🧠 SYSTEM INTELLIGENCE REPORT — ${today}\\n\\n`;
report += `📊 System Health: ${aiInsight.system_health}\\n`;
report += `📈 Learning Trend: ${aiInsight.learning_rate_trend}\\n`;
report += `💰 Revenue Efficiency: ${aiInsight.revenue_efficiency}\\n\\n`;

report += `📬 Activity: ${ctx.totalMemos} memos | ${ctx.croEvals} CRO evals (avg ${ctx.avgCroScore}/100)\\n`;
report += `✅ Approved: ${ctx.projApproved} | ❌ Rejected: ${ctx.projRejected} | 💵 Revenue: ${ctx.revToday}\\n\\n`;

report += `🥇 Best Agent: ${ctx.bestAgent}\\n`;
report += `⚠️ Underperforming: ${ctx.worstAgent}\\n\\n`;

if (aiInsight.pattern_alerts && aiInsight.pattern_alerts.length > 0) {
    report += `🔁 PATTERNS DETECTED:\\n`;
    aiInsight.pattern_alerts.forEach(p => report += `  • ${p}\\n`);
    report += '\\n';
}

if (aiInsight.adaptation_recommendations && aiInsight.adaptation_recommendations.length > 0) {
    report += `🔧 RECOMMENDATIONS FOR TOMORROW:\\n`;
    aiInsight.adaptation_recommendations.forEach(r => report += `  • ${r}\\n`);
    report += '\\n';
}

if (aiInsight.ai_insight) {
    report += `🤖 AI INSIGHT:\\n${aiInsight.ai_insight}\\n`;
}

return [{ json: { telegram_message: report }}];"""

META_INTEL_SQL = """SELECT
    (SELECT COUNT(*) FROM bridge.agent_memos WHERE created_at >= NOW() - INTERVAL '24h') AS total_memos,
    (SELECT COUNT(*) FROM bridge.cro_evaluations WHERE created_at >= NOW() - INTERVAL '24h') AS cro_evaluations,
    (SELECT ROUND(AVG(cro_score), 1) FROM bridge.cro_evaluations WHERE created_at >= NOW() - INTERVAL '24h') AS avg_cro_score,
    (SELECT COUNT(*) FROM bridge.project_memory WHERE outcome = 'approved' AND updated_at >= NOW() - INTERVAL '24h') AS projects_approved,
    (SELECT COUNT(*) FROM bridge.project_memory WHERE outcome = 'rejected' AND updated_at >= NOW() - INTERVAL '24h') AS projects_rejected,
    (SELECT COALESCE(SUM(actual_revenue_usd), 0) FROM bridge.project_memory WHERE updated_at >= NOW() - INTERVAL '24h') AS revenue_today,
    (SELECT agent_name FROM bridge.agent_performance WHERE date = CURRENT_DATE ORDER BY tasks_success DESC LIMIT 1) AS best_agent,
    (SELECT agent_name FROM bridge.agent_performance WHERE date = CURRENT_DATE AND tasks_total > 0 ORDER BY (tasks_success::float / NULLIF(tasks_total,0)) ASC LIMIT 1) AS worst_agent"""


def build_meta_intelligence():
    """Build the 6-node Meta-Intelligence workflow JSON."""
    wf_id = str(uuid.uuid4())

    def node(name, ntype, position, params):
        return {"id": str(uuid.uuid4()), "name": name, "type": ntype,
                "typeVersion": 1, "position": position, "parameters": params}

    nodes = [
        node("Schedule: 23:30 UTC", "n8n-nodes-base.scheduleTrigger", [240, 300], {
            "cronExpression": "30 23 * * *"
        }),
        node("Pull 24h data", "n8n-nodes-base.postgres", [480, 300], {
            "operation": "executeQuery",
            "query": META_INTEL_SQL
        }),
        node("Build AI prompt", "n8n-nodes-base.code", [720, 300], {
            "jsCode": META_INTEL_BUILD_CODE
        }),
        node("Call super-agent", "n8n-nodes-base.httpRequest", [960, 300], {
            "method": "POST",
            "url": "={{ $env.SUPER_AGENT_URL || 'https://super-agent-production.up.railway.app' }}/chat/direct",
            "sendBody": True,
            "bodyParameters": {
                "parameters": [
                    {"name": "message",    "value": "={{ $json.prompt }}"},
                    {"name": "model",      "value": "CLAUDE"},
                    {"name": "session_id", "value": "=bridge-meta-intel-{{ $now.format('yyyyMMdd') }}"},
                    {"name": "force_json", "value": "true"}
                ]
            },
            "responseFormat": "json"
        }),
        node("Build intelligence report", "n8n-nodes-base.code", [1200, 300], {
            "jsCode": META_INTEL_REPORT_CODE
        }),
        node("Send to CEO + COS", "n8n-nodes-base.httpRequest", [1440, 300], {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.BRIDGE_CEO_BOT_TOKEN }}/sendMessage",
            "sendBody": True,
            "bodyParameters": {
                "parameters": [
                    {"name": "chat_id", "value": "={{ $env.CEO_TELEGRAM_CHAT_ID }}"},
                    {"name": "text",    "value": "={{ $json.telegram_message }}"},
                    {"name": "parse_mode", "value": "Markdown"}
                ]
            }
        }),
    ]

    connections = {
        "Schedule: 23:30 UTC": {"main": [[{"node": "Pull 24h data", "type": "main", "index": 0}]]},
        "Pull 24h data":        {"main": [[{"node": "Build AI prompt", "type": "main", "index": 0}]]},
        "Build AI prompt":      {"main": [[{"node": "Call super-agent", "type": "main", "index": 0}]]},
        "Call super-agent":     {"main": [[{"node": "Build intelligence report", "type": "main", "index": 0}]]},
        "Build intelligence report": {"main": [[{"node": "Send to CEO + COS", "type": "main", "index": 0}]]},
    }

    return {
        "id": wf_id, "name": "Bridge_Meta_Intelligence",
        "active": True, "nodes": nodes, "connections": connections,
        "settings": {"executionOrder": "v1"}
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("Bridge OS — Intelligence Layer Upgrade")
    print("=" * 60)

    print("\n[Step 2+3] Patching Bridge bots...")
    for filename, bot_key, is_cro, add_bizdev, add_ceo, add_cos in BOTS:
        filepath = os.path.join(N8N_DIR, filename)
        if not os.path.exists(filepath):
            print(f"  SKIP (not found): {filename}")
            continue

        bot = load(filename)
        modified = False

        # Patch Assemble prompt — auto-block + (CRO: memory query)
        assemble = find_node(bot, "Assemble prompt")
        if assemble:
            orig = assemble["parameters"].get("jsCode", "")
            if "_autoBlockActive" not in orig and "_historicalContext" not in orig:
                if is_cro:
                    # Full CRO memory block + system prompt replacement
                    new_code = build_cro_assemble_code(orig)
                    # Replace system prompt to full gating version
                    import re as _re
                    system_match = _re.search(r'const system = "(.*?)";', new_code, _re.DOTALL)
                    if system_match:
                        new_code = new_code[:system_match.start()] + \
                                   f'const system = {json.dumps(CRO_SYSTEM_FULL)};' + \
                                   new_code[system_match.end():]
                else:
                    new_code = patch_assemble_prompt_autoblock(orig, is_cro=False)
                assemble["parameters"]["jsCode"] = new_code
                modified = True
                print(f"  {filename}: Assemble prompt patched (auto-block{'+ CRO gating' if is_cro else ''})")

        # Patch Execute SQL — add performance self-report CTE
        execute = find_node(bot, "Execute low-risk action")
        if execute and "query" in execute.get("parameters", {}):
            orig_sql = execute["parameters"]["query"]
            if "perf_self_report" not in orig_sql:
                execute["parameters"]["query"] = patch_execute_sql(orig_sql, bot_key, is_cro)
                modified = True
                print(f"  {filename}: Execute SQL patched (perf_self_report CTE{'+ cro_eval_log' if is_cro else ''})")

        # Patch system prompts for routing chain
        if add_bizdev or add_ceo or add_cos:
            assemble = find_node(bot, "Assemble prompt")
            if assemble:
                code = assemble["parameters"].get("jsCode", "")
                addition = (BIZDEV_ROUTING_ADDITION if add_bizdev
                            else CEO_CRO_GATE_ADDITION if add_ceo
                            else COS_CHAIN_ADDITION)
                tag = ("PROPOSAL ROUTING TO CRO" if add_bizdev
                       else "CRO GATE (MANDATORY" if add_ceo
                       else "APPROVAL CHAIN ENFORCEMENT")
                if tag not in code:
                    # Find the system string and append
                    import re as _re
                    # Pattern: const system = "..."; — replace closing "; with addition + ";
                    system_match = _re.search(r'(const system = (?:"|`)[^`"]*?)("|\`;)', code)
                    if system_match:
                        quote_char = system_match.group(2)
                        insert_pos = system_match.end(1)
                        new_addition_escaped = json.dumps(addition)[1:-1]  # escape for string insertion
                        code = code[:insert_pos] + new_addition_escaped + code[insert_pos:]
                        assemble["parameters"]["jsCode"] = code
                        modified = True
                        label = "bizdev CRO routing" if add_bizdev else "CEO CRO gate" if add_ceo else "COS chain enforcement"
                        print(f"  {filename}: system prompt patched ({label})")

        if modified:
            save(bot, filename)
        else:
            print(f"  {filename}: already up-to-date, skipped")

    print("\n[Step 4] Creating Performance Tracker workflow...")
    perf_tracker = build_performance_tracker()
    save(perf_tracker, "bridge_performance_tracker.json")
    print(f"  Nodes: {len(perf_tracker['nodes'])}")

    print("\n[Step 5] Creating Meta-Intelligence workflow...")
    meta_intel = build_meta_intelligence()
    save(meta_intel, "bridge_meta_intelligence.json")
    print(f"  Nodes: {len(meta_intel['nodes'])}")

    print("\nIntelligence Layer Upgrade complete.")
    print("   Next: commit + push + deploy, then run the SQL schema on Railway PostgreSQL.")


if __name__ == "__main__":
    main()
