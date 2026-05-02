"""
Bridge OS V2 Architecture Upgrade

Fixes and additions:
1. task_id generation in ALL bots' Build task payload (V2 Task Envelope)
2. task_id propagated into contextBlock so LLM includes it in all memos
3. CEO bot Execute SQL bug fix (invalid multi-column subquery)
4. Security tier language (SAFE/RESTRICTED/CRITICAL) added to COS + Security bots
5. Finance ROI extensions added to Finance bot system prompt
6. Creates bridge_self_healing.json (detect + repair broken tasks)
7. Creates bridge_anomaly_detector.json (cost spikes, loops, drift)
8. Creates bridge_cro_revenue_engine.json (daily revenue opportunities)
9. Creates n8n/schema/bridge_v2_schema.sql (task_ledger + anomaly_log)
"""
import json, os, re

N8N_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "n8n")
SCHEMA_DIR = os.path.join(N8N_DIR, "schema")
os.makedirs(SCHEMA_DIR, exist_ok=True)

# ─── 1. task_id injection into Build task payload ────────────────────────────
# Injected at the TOP of jsCode (before loop prevention) to capture/generate task_id
# and at the END in the return statement to include it.

TASK_ID_INJECT_TOP = (
    "// ── V2 Task Envelope: task_id propagation ─────────────────────────────────────\n"
    "const _env = (typeof $input !== 'undefined' && $input.all()[0].json) || {};\n"
    "const _envBody = _env.body || {};\n"
    "const _inherited_task_id = _envBody.task_id || null;\n"
    "const _gen_task_id = () => 'tid-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 9);\n"
    "const _task_id = _inherited_task_id || _gen_task_id();\n"
    "// ── End V2 Task Envelope ────────────────────────────────────────────────────────\n\n"
)

# In the return statement, we need to add task_id to the output.
# The return always ends with `} }];` — we find the last one and add task_id there.

def inject_task_id(code, bot_name):
    """Inject task_id generation at top and propagation into context + return."""
    if "_task_id" in code:
        print(f"  {bot_name}: task_id already present, skipping")
        return code, False

    # 1. Inject at very top (before the loop prevention guard)
    code = TASK_ID_INJECT_TOP + code

    # 2. Add task_id to the final return object
    # Find the return statement — it always has: session_id: `bridge-...`
    # and ends with `} }];` — insert task_id before the closing } }]
    return_marker = "return [{ json: {"
    last_return = code.rfind(return_marker)
    if last_return == -1:
        print(f"  {bot_name}: WARNING — return statement not found for task_id")
        return code, False

    # Find the closing `} }];` after the return
    close_idx = code.find("} }];", last_return)
    if close_idx == -1:
        print(f"  {bot_name}: WARNING — closing of return not found")
        return code, False

    # Insert task_id before closing
    code = code[:close_idx] + "    task_id: _task_id,\n" + code[close_idx:]

    # 3. Add task_id to contextBlock so LLM sees it
    # contextBlock is JSON.stringify({...}) — we add task_id to it
    ctx_marker = "const contextBlock = JSON.stringify({"
    ctx_idx = code.find(ctx_marker)
    if ctx_idx != -1:
        # Find the first key after the opening brace
        inner_start = code.find("{", ctx_idx + len(ctx_marker) - 1) + 1
        code = code[:inner_start] + "\n    task_id: _task_id," + code[inner_start:]
        print(f"  {bot_name}: task_id added to contextBlock")

    print(f"  {bot_name}: task_id injected")
    return code, True


# ─── 2. CEO bot SQL fix ──────────────────────────────────────────────────────
CEO_SQL_OLD = (
    "(SELECT memo_id,\n"
    "       (SELECT cos_memo_id FROM cos_trigger) AS cos_triggered FROM memo_insert)  AS memo_created,"
)
CEO_SQL_FIX = (
    "(SELECT memo_id FROM memo_insert) AS memo_created,\n"
    "  (SELECT cos_memo_id FROM cos_trigger) AS cos_triggered,"
)

# ─── 3. System prompt additions ──────────────────────────────────────────────

SECURITY_TIERS_ADDITION = (
    "\n\nSECURITY TIER ENFORCEMENT (3-tier system, non-negotiable):\n"
    "Every action has a risk level that maps to a security tier:\n\n"
    "🟢 SAFE (low risk — auto-approve, no notification required):\n"
    "   Informational queries, status updates, memo reads/writes, internal logging.\n"
    "   System executes immediately without waiting for approval.\n\n"
    "🟡 RESTRICTED (medium risk — requires human approval via Telegram):\n"
    "   Any write to external systems, financial transactions > $50,\n"
    "   new outreach campaigns, API calls to third parties, configuration changes.\n"
    "   System sends Telegram prompt and waits. If no response in 5 days → fallback vote.\n\n"
    "🔴 CRITICAL (high risk — requires owner passcode, NEVER auto-approved):\n"
    "   Irreversible actions, production deployments, budget changes > $500,\n"
    "   data deletions, infrastructure changes.\n"
    "   Security veto power applies. If Security rejects → BLOCKED, no vote override.\n\n"
    "When routing any action: always classify the security tier FIRST.\n"
    "If classification is unclear → default to RESTRICTED (safer tier).\n"
    "NEVER execute a CRITICAL action without verified human authorization."
)

FINANCE_ROI_ADDITION = (
    "\n\nFINANCE ROI TRACKING (mandatory extensions):\n"
    "For every expense or cost record, include task_id from the triggering memo's body_json.\n"
    "Calculate and report:\n"
    "  - cost_per_task: total expenses tagged to each task_id\n"
    "  - revenue_per_task: actual_revenue_usd from bridge.project_memory for completed tasks\n"
    "  - roi_per_task: (revenue - cost) / cost * 100 as percentage\n"
    "  - cost_per_conversion: total outreach cost / paid_invoices count\n\n"
    "WEEKLY ROI REPORT FORMAT (add to weekly Finance report):\n"
    "  💰 TOTAL COST THIS WEEK: $X\n"
    "  📈 REVENUE ATTRIBUTED: $X\n"
    "  📊 ROI: X%\n"
    "  🏆 BEST ROI TASK: [task_id] - X%\n"
    "  ⚠️ HIGHEST COST TASKS: [list top 3 by cost with no revenue attributed]\n"
    "  💡 EFFICIENCY: $X cost per conversion\n\n"
    "Flag any task where cost > $100 and revenue_attributed = 0 to COS immediately.\n"
    "Flag any agent where total spend > 2x their revenue contribution to CEO weekly."
)

# ─── 4. New workflow: Self-Healing System ────────────────────────────────────

SELF_HEALING_WF = {
    "name": "Bridge_Self_Healing_System",
    "nodes": [
        {
            "id": "sh-trigger",
            "name": "Schedule: every 30min",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [240, 300],
            "parameters": {
                "rule": {"interval": [{"field": "cronExpression", "expression": "0 */30 * * * *"}]}
            }
        },
        {
            "id": "sh-stalled",
            "name": "Find stalled tasks",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 200],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT memo_id, from_agent, to_agent, memo_type, subject, body_json,\n"
                    "       created_at, EXTRACT(EPOCH FROM (NOW()-created_at))/3600 AS hours_open\n"
                    "FROM bridge.agent_memos\n"
                    "WHERE status = 'open'\n"
                    "  AND memo_type IN ('execution_directive','cto_review_request','build_validation_request')\n"
                    "  AND created_at < NOW() - INTERVAL '2 hours'\n"
                    "  AND (body_json->>'self_heal_flagged') IS NULL\n"
                    "ORDER BY created_at ASC LIMIT 10;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "sh-failures",
            "name": "Find recent failures",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 400],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT workflow_name, event_type, details_json, created_at,\n"
                    "       COUNT(*) OVER (PARTITION BY workflow_name) AS failure_count\n"
                    "FROM bridge.workflow_events\n"
                    "WHERE event_type ILIKE '%error%' OR event_type ILIKE '%fail%'\n"
                    "  AND created_at >= NOW() - INTERVAL '1 hour'\n"
                    "ORDER BY created_at DESC LIMIT 20;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "sh-analyze",
            "name": "Analyze + classify issues",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [720, 300],
            "parameters": {
                "jsCode": (
                    "const stalled = $('Find stalled tasks').all().map(i => i.json).filter(r => r.memo_id);\n"
                    "const failures = $('Find recent failures').all().map(i => i.json).filter(r => r.workflow_name);\n\n"
                    "const issues = [];\n\n"
                    "// Classify stalled tasks\n"
                    "for (const t of stalled) {\n"
                    "    const hours = parseFloat(t.hours_open) || 0;\n"
                    "    const severity = hours > 12 ? 'critical' : hours > 4 ? 'high' : 'medium';\n"
                    "    issues.push({\n"
                    "        type: 'stalled_task', severity, memo_id: t.memo_id,\n"
                    "        from_agent: t.from_agent, to_agent: t.to_agent,\n"
                    "        subject: t.subject, hours_open: hours,\n"
                    "        fix: t.memo_type === 'cto_review_request' ? 'cto_nudge' : 'cos_escalate'\n"
                    "    });\n"
                    "}\n\n"
                    "// Classify repeated failures\n"
                    "const wfFailMap = {};\n"
                    "for (const f of failures) {\n"
                    "    wfFailMap[f.workflow_name] = (wfFailMap[f.workflow_name] || 0) + 1;\n"
                    "}\n"
                    "for (const [wf, count] of Object.entries(wfFailMap)) {\n"
                    "    if (count >= 2) {\n"
                    "        issues.push({\n"
                    "            type: 'repeated_failure', severity: count >= 5 ? 'critical' : 'high',\n"
                    "            workflow_name: wf, failure_count: count,\n"
                    "            fix: 'cto_architecture_review'\n"
                    "        });\n"
                    "    }\n"
                    "}\n\n"
                    "const critical = issues.filter(i => i.severity === 'critical');\n"
                    "const high = issues.filter(i => i.severity === 'high');\n"
                    "const total = issues.length;\n\n"
                    "if (total === 0) return [{ json: { no_issues: true } }];\n\n"
                    "return [{ json: { issues, critical_count: critical.length, high_count: high.length,\n"
                    "    summary: `${total} issues: ${critical.length} critical, ${high.length} high` } }];\n"
                )
            }
        },
        {
            "id": "sh-if-issues",
            "name": "Issues found?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.2,
            "position": [960, 300],
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "typeValidation": "strict"},
                    "conditions": [{"id": "has_issues", "leftValue": "={{ $json.no_issues }}", "rightValue": True,
                                    "operator": {"type": "boolean", "operation": "notTrue"}}],
                    "combinator": "and"
                },
                "options": {}
            }
        },
        {
            "id": "sh-repair",
            "name": "Create repair memos",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [1200, 200],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "WITH mark_flagged AS (\n"
                    "  UPDATE bridge.agent_memos\n"
                    "  SET body_json = body_json || '{\"self_heal_flagged\": true}'::jsonb\n"
                    "  WHERE memo_id = ANY(\n"
                    "    SELECT (issue->>'memo_id')::uuid\n"
                    "    FROM jsonb_array_elements($1::jsonb) AS issue\n"
                    "    WHERE issue->>'memo_id' IS NOT NULL\n"
                    "  )\n"
                    "  RETURNING memo_id\n"
                    "),\n"
                    "alert_cos AS (\n"
                    "  INSERT INTO bridge.agent_memos\n"
                    "    (from_agent, to_agent, memo_type, priority, subject, body_json)\n"
                    "  VALUES (\n"
                    "    'self_healing', 'chief_of_staff', 'system_health_alert', 'high',\n"
                    "    $2,\n"
                    "    jsonb_build_object('issues', $1::jsonb, 'auto_generated', true,\n"
                    "                       'action_required', 'review_and_dispatch_fixes')\n"
                    "  ) RETURNING memo_id\n"
                    "),\n"
                    "log_heal AS (\n"
                    "  INSERT INTO bridge.workflow_events (workflow_name, event_type, details_json)\n"
                    "  VALUES ('self_healing', 'issues_detected',\n"
                    "    jsonb_build_object('summary', $2, 'issue_count', jsonb_array_length($1::jsonb)))\n"
                    "  RETURNING event_id\n"
                    ")\n"
                    "SELECT (SELECT COUNT(*) FROM mark_flagged) AS flagged,\n"
                    "       (SELECT memo_id FROM alert_cos) AS cos_alerted;"
                ),
                "options": {
                    "queryReplacement": "={{ JSON.stringify($json.issues) }},={{ $json.summary }}"
                }
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "sh-alert",
            "name": "Alert CEO if critical",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [1200, 400],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "INSERT INTO bridge.agent_memos\n"
                    "  (from_agent, to_agent, memo_type, priority, subject, body_json)\n"
                    "SELECT 'self_healing', 'ceo', 'critical_system_alert', 'urgent',\n"
                    "       'CRITICAL: ' || $1,\n"
                    "       jsonb_build_object('critical_issues', $2::jsonb,\n"
                    "                          'requires_immediate_action', true)\n"
                    "WHERE $3::int > 0\n"
                    "RETURNING memo_id;"
                ),
                "options": {
                    "queryReplacement": "={{ $json.summary }},={{ JSON.stringify($json.issues.filter(i=>i.severity==='critical')) }},={{ $json.critical_count }}"
                }
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        }
    ],
    "connections": {
        "Schedule: every 30min": {"main": [[
            {"node": "Find stalled tasks", "type": "main", "index": 0},
            {"node": "Find recent failures", "type": "main", "index": 0}
        ]]},
        "Find stalled tasks":  {"main": [[{"node": "Analyze + classify issues", "type": "main", "index": 0}]]},
        "Find recent failures": {"main": [[{"node": "Analyze + classify issues", "type": "main", "index": 0}]]},
        "Analyze + classify issues": {"main": [[{"node": "Issues found?", "type": "main", "index": 0}]]},
        "Issues found?": {"main": [
            [{"node": "Create repair memos", "type": "main", "index": 0},
             {"node": "Alert CEO if critical", "type": "main", "index": 0}],
            []
        ]}
    },
    "settings": {"executionOrder": "v1"}, "pinData": None
}


# ─── 5. New workflow: Anomaly Detector ───────────────────────────────────────

ANOMALY_DETECTOR_WF = {
    "name": "Bridge_Anomaly_Detector",
    "nodes": [
        {
            "id": "ad-trigger",
            "name": "Schedule: every hour",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [240, 300],
            "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 0 * * * *"}]}}
        },
        {
            "id": "ad-costs",
            "name": "Cost spike check",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 200],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "WITH hourly_avg AS (\n"
                    "  SELECT COALESCE(AVG(hourly_cost), 0) AS avg_cost\n"
                    "  FROM (\n"
                    "    SELECT DATE_TRUNC('hour', occurred_at) AS h, SUM(amount_usd) AS hourly_cost\n"
                    "    FROM bridge.expenses\n"
                    "    WHERE occurred_at >= NOW() - INTERVAL '7 days'\n"
                    "    GROUP BY h\n"
                    "  ) t\n"
                    "),\n"
                    "last_hour AS (\n"
                    "  SELECT COALESCE(SUM(amount_usd), 0) AS cost\n"
                    "  FROM bridge.expenses\n"
                    "  WHERE occurred_at >= NOW() - INTERVAL '1 hour'\n"
                    ")\n"
                    "SELECT last_hour.cost AS last_hour_cost, hourly_avg.avg_cost,\n"
                    "  CASE WHEN hourly_avg.avg_cost > 0\n"
                    "       THEN ROUND(last_hour.cost / hourly_avg.avg_cost, 2)\n"
                    "       ELSE 0 END AS spike_ratio,\n"
                    "  last_hour.cost > hourly_avg.avg_cost * 3 AS is_spike\n"
                    "FROM last_hour, hourly_avg;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "ad-loops",
            "name": "Loop storm check",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 350],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT\n"
                    "  COUNT(*) FILTER (WHERE event_type = 'loop_detected') AS loops_last_30min,\n"
                    "  COUNT(*) FILTER (WHERE event_type = 'auto_block_repeated_rejection') AS autoblock_last_30min,\n"
                    "  COUNT(*) FILTER (WHERE event_type ILIKE '%error%') AS errors_last_30min,\n"
                    "  COUNT(*) > 5 AS is_loop_storm\n"
                    "FROM bridge.workflow_events\n"
                    "WHERE created_at >= NOW() - INTERVAL '30 minutes'\n"
                    "  AND event_type IN ('loop_detected','auto_block_repeated_rejection','error','parse_failed');"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "ad-drift",
            "name": "Agent drift check",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 500],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT agent_name,\n"
                    "  today.success_rate AS today_rate,\n"
                    "  week_avg.avg_rate AS week_avg_rate,\n"
                    "  today.success_rate < week_avg.avg_rate - 20 AS is_drifting,\n"
                    "  today.tasks_total,\n"
                    "  authority_level\n"
                    "FROM (\n"
                    "  SELECT agent_name, authority_level,\n"
                    "    ROUND(100.0 * tasks_success / NULLIF(tasks_total, 0), 1) AS success_rate,\n"
                    "    tasks_total\n"
                    "  FROM bridge.agent_performance WHERE date = CURRENT_DATE AND tasks_total >= 3\n"
                    ") today\n"
                    "JOIN (\n"
                    "  SELECT agent_name,\n"
                    "    ROUND(AVG(100.0 * tasks_success / NULLIF(tasks_total, 0)), 1) AS avg_rate\n"
                    "  FROM bridge.agent_performance\n"
                    "  WHERE date >= CURRENT_DATE - INTERVAL '7 days'\n"
                    "  GROUP BY agent_name\n"
                    ") week_avg USING (agent_name)\n"
                    "WHERE today.success_rate < week_avg.avg_rate - 20;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "ad-volume",
            "name": "Memo volume spike",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 650],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "WITH hourly_avg AS (\n"
                    "  SELECT COALESCE(AVG(cnt), 0) AS avg_memos\n"
                    "  FROM (\n"
                    "    SELECT DATE_TRUNC('hour', created_at) AS h, COUNT(*) AS cnt\n"
                    "    FROM bridge.agent_memos\n"
                    "    WHERE created_at >= NOW() - INTERVAL '7 days'\n"
                    "    GROUP BY h\n"
                    "  ) t\n"
                    "),\n"
                    "last_hour AS (\n"
                    "  SELECT COUNT(*) AS cnt FROM bridge.agent_memos\n"
                    "  WHERE created_at >= NOW() - INTERVAL '1 hour'\n"
                    ")\n"
                    "SELECT last_hour.cnt AS last_hour_memos, ROUND(hourly_avg.avg_memos) AS avg_memos,\n"
                    "  last_hour.cnt > hourly_avg.avg_memos * 5 AS is_volume_spike\n"
                    "FROM last_hour, hourly_avg;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "ad-build",
            "name": "Build anomaly report",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [760, 420],
            "parameters": {
                "jsCode": (
                    "const costs = $('Cost spike check').first().json || {};\n"
                    "const loops = $('Loop storm check').first().json || {};\n"
                    "const drifters = $('Agent drift check').all().map(i => i.json).filter(r => r.agent_name && r.is_drifting);\n"
                    "const volume = $('Memo volume spike').first().json || {};\n\n"
                    "const anomalies = [];\n"
                    "if (costs.is_spike) anomalies.push({ type: 'cost_spike', severity: 'critical',\n"
                    "  detail: `Cost ${costs.spike_ratio}x normal (${costs.last_hour_cost} vs avg ${costs.avg_cost})` });\n"
                    "if (loops.is_loop_storm) anomalies.push({ type: 'loop_storm', severity: 'critical',\n"
                    "  detail: `${loops.loops_last_30min} loops + ${loops.errors_last_30min} errors in 30min` });\n"
                    "if (volume.is_volume_spike) anomalies.push({ type: 'memo_volume_spike', severity: 'high',\n"
                    "  detail: `${volume.last_hour_memos} memos vs avg ${volume.avg_memos}` });\n"
                    "for (const d of drifters) anomalies.push({ type: 'agent_drift', severity: 'high',\n"
                    "  agent: d.agent_name, detail: `${d.today_rate}% vs ${d.week_avg_rate}% week avg` });\n\n"
                    "if (anomalies.length === 0) return [{ json: { no_anomalies: true } }];\n\n"
                    "const critical = anomalies.filter(a => a.severity === 'critical');\n"
                    "const report = anomalies.map(a => `• [${a.severity.toUpperCase()}] ${a.type}: ${a.detail}`).join('\\n');\n"
                    "return [{ json: { anomalies, critical_count: critical.length,\n"
                    "  report, no_anomalies: false } }];\n"
                )
            }
        },
        {
            "id": "ad-if",
            "name": "Anomalies found?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.2,
            "position": [1000, 420],
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "typeValidation": "strict"},
                    "conditions": [{"id": "has_anomalies",
                                    "leftValue": "={{ $json.no_anomalies }}", "rightValue": True,
                                    "operator": {"type": "boolean", "operation": "notTrue"}}],
                    "combinator": "and"
                },
                "options": {}
            }
        },
        {
            "id": "ad-log",
            "name": "Log + alert on anomalies",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [1240, 320],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "WITH log_anomaly AS (\n"
                    "  INSERT INTO bridge.workflow_events (workflow_name, event_type, details_json)\n"
                    "  VALUES ('anomaly_detector', 'anomalies_detected',\n"
                    "    jsonb_build_object('anomalies', $1::jsonb, 'critical_count', $3::int))\n"
                    "  RETURNING event_id\n"
                    "),\n"
                    "alert_cos AS (\n"
                    "  INSERT INTO bridge.agent_memos\n"
                    "    (from_agent, to_agent, memo_type, priority, subject, body_json)\n"
                    "  VALUES (\n"
                    "    'anomaly_detector', 'chief_of_staff',\n"
                    "    CASE WHEN $3::int > 0 THEN 'critical_system_alert' ELSE 'system_health_alert' END,\n"
                    "    CASE WHEN $3::int > 0 THEN 'urgent' ELSE 'high' END,\n"
                    "    'Anomaly detected: ' || $2,\n"
                    "    jsonb_build_object('anomalies', $1::jsonb, 'report', $2)\n"
                    "  ) RETURNING memo_id\n"
                    ")\n"
                    "SELECT (SELECT event_id FROM log_anomaly) AS logged,\n"
                    "       (SELECT memo_id FROM alert_cos) AS alerted;"
                ),
                "options": {
                    "queryReplacement": "={{ JSON.stringify($json.anomalies) }},={{ $json.report }},={{ $json.critical_count }}"
                }
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        }
    ],
    "connections": {
        "Schedule: every hour": {"main": [[
            {"node": "Cost spike check", "type": "main", "index": 0},
            {"node": "Loop storm check", "type": "main", "index": 0},
            {"node": "Agent drift check", "type": "main", "index": 0},
            {"node": "Memo volume spike", "type": "main", "index": 0}
        ]]},
        "Cost spike check":  {"main": [[{"node": "Build anomaly report", "type": "main", "index": 0}]]},
        "Loop storm check":  {"main": [[{"node": "Build anomaly report", "type": "main", "index": 0}]]},
        "Agent drift check": {"main": [[{"node": "Build anomaly report", "type": "main", "index": 0}]]},
        "Memo volume spike": {"main": [[{"node": "Build anomaly report", "type": "main", "index": 0}]]},
        "Build anomaly report": {"main": [[{"node": "Anomalies found?", "type": "main", "index": 0}]]},
        "Anomalies found?": {"main": [
            [{"node": "Log + alert on anomalies", "type": "main", "index": 0}], []
        ]}
    },
    "settings": {"executionOrder": "v1"}, "pinData": None
}


# ─── 6. New workflow: CRO Revenue Engine ─────────────────────────────────────

CRO_REVENUE_ENGINE_WF = {
    "name": "Bridge_CRO_Revenue_Engine",
    "nodes": [
        {
            "id": "cre-trigger",
            "name": "Schedule: daily 06:00",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [240, 300],
            "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 0 6 * * *"}]}}
        },
        {
            "id": "cre-pipeline",
            "name": "Pull revenue pipeline",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 200],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT\n"
                    "  marketing_status,\n"
                    "  COUNT(*) AS lead_count,\n"
                    "  COUNT(*) FILTER (WHERE updated_at < NOW() - INTERVAL '7 days') AS stalled_count,\n"
                    "  COUNT(*) FILTER (WHERE updated_at >= NOW() - INTERVAL '24 hours') AS active_24h\n"
                    "FROM bridge.leads\n"
                    "WHERE marketing_status NOT IN ('Paid','Closed','Rejected')\n"
                    "GROUP BY marketing_status\n"
                    "ORDER BY lead_count DESC;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "cre-gaps",
            "name": "Pull conversion gaps",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 400],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT\n"
                    "  l.lead_id, l.business_name, l.niche, l.city, l.marketing_status,\n"
                    "  l.updated_at,\n"
                    "  EXTRACT(DAY FROM NOW() - l.updated_at)::int AS days_stalled,\n"
                    "  (SELECT COUNT(*) FROM bridge.outreach_messages o\n"
                    "   WHERE o.lead_id = l.lead_id AND o.direction = 'outbound'\n"
                    "     AND o.sent_at >= NOW() - INTERVAL '7 days') AS recent_touches\n"
                    "FROM bridge.leads l\n"
                    "WHERE l.marketing_status IN ('Contacted','Interested','Proposal Sent')\n"
                    "  AND l.updated_at < NOW() - INTERVAL '7 days'\n"
                    "  AND l.finance_status != 'Paid'\n"
                    "ORDER BY days_stalled DESC\n"
                    "LIMIT 10;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "cre-wins",
            "name": "Pull recent wins + losses",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 600],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT\n"
                    "  outcome, COUNT(*) AS count,\n"
                    "  ROUND(AVG(cro_score)::numeric, 1) AS avg_cro_score,\n"
                    "  ROUND(AVG(actual_revenue_usd)::numeric, 2) AS avg_revenue,\n"
                    "  array_agg(project_name ORDER BY updated_at DESC) FILTER (WHERE project_name IS NOT NULL)\n"
                    "    AS recent_projects\n"
                    "FROM bridge.project_memory\n"
                    "WHERE updated_at >= NOW() - INTERVAL '14 days'\n"
                    "GROUP BY outcome;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "cre-prompt",
            "name": "Assemble CRO analysis",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [760, 400],
            "parameters": {
                "jsCode": (
                    "const pipeline = $('Pull revenue pipeline').all().map(i => i.json);\n"
                    "const gaps = $('Pull conversion gaps').all().map(i => i.json).filter(r => r.lead_id);\n"
                    "const performance = $('Pull recent wins + losses').all().map(i => i.json);\n\n"
                    "const today = new Date().toISOString().slice(0, 10);\n"
                    "const stalledLeads = gaps.length;\n"
                    "const interestedCount = pipeline.find(p => p.marketing_status === 'Interested')?.lead_count || 0;\n"
                    "const contactedCount = pipeline.find(p => p.marketing_status === 'Contacted')?.lead_count || 0;\n\n"
                    "const prompt = `You are Bridge_CRO_BOT running the daily revenue engine. Today is ${today}.\n\nREVENUE PIPELINE:\n${JSON.stringify(pipeline, null, 2)}\n\nSTALLED CONVERSIONS (no touch in 7+ days):\n${JSON.stringify(gaps.slice(0, 5), null, 2)}\n\nRECENT PROJECT OUTCOMES:\n${JSON.stringify(performance, null, 2)}\n\nYour job: Generate 3-5 specific, actionable revenue opportunities. For each opportunity:\n1. Identify a concrete action (re-engage stalled lead, upsell existing client, new campaign)\n2. Estimate revenue potential ($)\n3. Identify which agent should execute\n4. Provide a specific memo subject and brief action body\n\nFocus on highest-ROI, lowest-effort opportunities first.\nReturn a JSON array of opportunities:\n[{\"priority\": 1, \"type\": \"re_engage|upsell|new_campaign\", \"target\": \"lead_id or general\", \"revenue_potential_usd\": 0, \"effort\": \"low|medium|high\", \"execute_agent\": \"bizdev|marketing|pm\", \"memo_subject\": \"...\", \"memo_body\": \"...\"}]`;\n\n"
                    "return [{ json: { prompt, stalled_count: stalledLeads, interested_count: interestedCount,\n"
                    "  contacted_count: contactedCount, today } }];\n"
                )
            }
        },
        {
            "id": "cre-llm",
            "name": "Call CRO analysis",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [1000, 400],
            "parameters": {
                "method": "POST",
                "url": "={{ $env.SUPER_AGENT_URL || 'https://super-agent-production.up.railway.app' }}/chat/direct",
                "sendHeaders": True,
                "headerParameters": {"parameters": [
                    {"name": "X-Token", "value": "={{ $env.SUPER_AGENT_PASSWORD }}"},
                    {"name": "Content-Type", "value": "application/json"}
                ]},
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": '={ "message": {{ JSON.stringify($json.prompt) }}, "model": "CLAUDE", "session_id": "bridge-cro-revenue-engine-{{ $now.format(\'yyyyMMdd\') }}" }',
                "options": {"timeout": 90000, "response": {"response": {"neverError": True}}}
            }
        },
        {
            "id": "cre-parse",
            "name": "Parse + dispatch opportunities",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1240, 400],
            "parameters": {
                "jsCode": (
                    "const raw = $json.response || '';\n"
                    "const ctx = $('Assemble CRO analysis').first().json;\n\n"
                    "let opportunities = [];\n"
                    "try {\n"
                    "  const cleaned = raw.trim().replace(/^```(?:json)?/, '').replace(/```$/, '').trim();\n"
                    "  const m = cleaned.match(/\\[[\\s\\S]*\\]/);\n"
                    "  if (m) opportunities = JSON.parse(m[0]);\n"
                    "} catch(e) { opportunities = []; }\n\n"
                    "// Keep top 3 by priority\n"
                    "const top = opportunities.slice(0, 3);\n"
                    "return top.map(o => ({ json: {\n"
                    "  to_agent: o.execute_agent || 'bizdev',\n"
                    "  memo_type: 'revenue_opportunity',\n"
                    "  priority: o.priority === 1 ? 'high' : 'normal',\n"
                    "  subject: o.memo_subject || 'CRO Revenue Opportunity',\n"
                    "  body_json: {\n"
                    "    ...o,\n"
                    "    source: 'cro_revenue_engine',\n"
                    "    generated_date: ctx.today,\n"
                    "    stalled_pipeline: ctx.stalled_count,\n"
                    "    attempt_count: 0\n"
                    "  }\n"
                    "}}));\n"
                )
            }
        },
        {
            "id": "cre-dispatch",
            "name": "Dispatch revenue memos",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [1480, 400],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "INSERT INTO bridge.agent_memos\n"
                    "  (from_agent, to_agent, memo_type, priority, subject, body_json)\n"
                    "VALUES ('cro', $1, $2, $3, $4, $5::jsonb)\n"
                    "RETURNING memo_id;"
                ),
                "options": {
                    "queryReplacement": (
                        "={{ $json.to_agent }},"
                        "={{ $json.memo_type }},"
                        "={{ $json.priority }},"
                        "={{ $json.subject }},"
                        "={{ JSON.stringify($json.body_json) }}"
                    )
                }
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        }
    ],
    "connections": {
        "Schedule: daily 06:00": {"main": [[
            {"node": "Pull revenue pipeline", "type": "main", "index": 0},
            {"node": "Pull conversion gaps", "type": "main", "index": 0},
            {"node": "Pull recent wins + losses", "type": "main", "index": 0}
        ]]},
        "Pull revenue pipeline":    {"main": [[{"node": "Assemble CRO analysis", "type": "main", "index": 0}]]},
        "Pull conversion gaps":     {"main": [[{"node": "Assemble CRO analysis", "type": "main", "index": 0}]]},
        "Pull recent wins + losses": {"main": [[{"node": "Assemble CRO analysis", "type": "main", "index": 0}]]},
        "Assemble CRO analysis": {"main": [[{"node": "Call CRO analysis", "type": "main", "index": 0}]]},
        "Call CRO analysis": {"main": [[{"node": "Parse + dispatch opportunities", "type": "main", "index": 0}]]},
        "Parse + dispatch opportunities": {"main": [[{"node": "Dispatch revenue memos", "type": "main", "index": 0}]]}
    },
    "settings": {"executionOrder": "v1"}, "pinData": None
}


# ─── Schema SQL ───────────────────────────────────────────────────────────────

V2_SCHEMA_SQL = """\
-- Bridge OS V2 Schema Extensions
-- Run against divine-contentment (bridge schema)

-- Task ledger: tracks every task_id from entry to completion
CREATE TABLE IF NOT EXISTS bridge.task_ledger (
    task_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'created',  -- created|validating|approved|executing|completed|failed|blocked
    current_agent TEXT,
    priority TEXT DEFAULT 'normal',
    requires_approval BOOLEAN DEFAULT FALSE,
    attempt_count INTEGER DEFAULT 0,
    cost_usd NUMERIC DEFAULT 0,
    revenue_usd NUMERIC DEFAULT 0,
    metadata JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_ledger_status ON bridge.task_ledger(status, created_at DESC);

-- Anomaly log: stores detected system anomalies
CREATE TABLE IF NOT EXISTS bridge.anomaly_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    anomaly_type TEXT NOT NULL,  -- cost_spike|loop_storm|agent_drift|volume_spike|repeated_failure
    severity TEXT NOT NULL,       -- critical|high|medium
    detail TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_anomaly_log_type ON bridge.anomaly_log(anomaly_type, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_log_unresolved ON bridge.anomaly_log(resolved, detected_at DESC) WHERE NOT resolved;

-- Add task_id column to agent_memos for tracking
DO $$ BEGIN
    ALTER TABLE bridge.agent_memos ADD COLUMN IF NOT EXISTS task_id TEXT REFERENCES bridge.task_ledger(task_id) ON DELETE SET NULL;
EXCEPTION WHEN others THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_agent_memos_task_id ON bridge.agent_memos(task_id) WHERE task_id IS NOT NULL;
"""

# ─── Main execution ───────────────────────────────────────────────────────────

# Load all bots to patch
BOT_FILES = [
    "bridge_pm_bot.json",
    "bridge_ceo_bot.json",
    "bridge_chief_of_staff_bot.json",
    "bridge_chief_revenue_optimizer_bot.json",
    "bridge_cto_bot.json",
    "bridge_researcher_bot.json",
    "bridge_programmer_bot.json",
    "bridge_cleaner_bot.json",
    "bridge_chief_sec_off_bot.json",
    "bridge_business_development_bot.json",
]

print("=== V2 Architecture Upgrade ===\n")

# 1. Patch task_id into all bots' Build task payload
print("--- task_id injection into Build task payload ---")
for fname in BOT_FILES:
    fpath = f"{N8N_DIR}/{fname}"
    if not os.path.exists(fpath):
        print(f"  SKIP {fname} (not found)")
        continue
    with open(fpath, encoding="utf-8") as f:
        d = json.load(f)
    changed = False
    for node in d["nodes"]:
        if node.get("name") == "Build task payload":
            new_code, patched = inject_task_id(node["parameters"]["jsCode"], fname)
            if patched:
                node["parameters"]["jsCode"] = new_code
                changed = True
    if changed:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"  Saved {fname}")
print()

# 2. Fix CEO bot SQL bug
print("--- CEO bot SQL fix ---")
ceo_path = f"{N8N_DIR}/bridge_ceo_bot.json"
with open(ceo_path, encoding="utf-8") as f:
    ceo_d = json.load(f)
ceo_raw = json.dumps(ceo_d, ensure_ascii=False)
if CEO_SQL_OLD.replace("\n", "\\n") in ceo_raw or CEO_SQL_OLD in json.dumps(ceo_d):
    for node in ceo_d["nodes"]:
        if node.get("name") == "Execute low-risk action":
            q = node["parameters"].get("query", "")
            q = q.replace(
                "(SELECT memo_id,\n       (SELECT cos_memo_id FROM cos_trigger) AS cos_triggered FROM memo_insert)  AS memo_created,",
                "(SELECT memo_id FROM memo_insert) AS memo_created,\n  (SELECT cos_memo_id FROM cos_trigger) AS cos_triggered,"
            )
            node["parameters"]["query"] = q
            print("  CEO Execute SQL bug fixed")
    with open(ceo_path, "w", encoding="utf-8") as f:
        json.dump(ceo_d, f, ensure_ascii=False, indent=2)
    print("  Saved bridge_ceo_bot.json")
else:
    print("  CEO SQL already OK (no double-comma bug found)")
print()

# 3. Add security tier language to COS
print("--- Security tier language patch ---")
cos_path = f"{N8N_DIR}/bridge_chief_of_staff_bot.json"
with open(cos_path, encoding="utf-8") as f:
    cos_d = json.load(f)

def add_to_prompt(nodes, bot_label, addition, marker):
    for node in nodes:
        if node.get("name") == "Assemble prompt":
            code = node["parameters"]["jsCode"]
            if marker in code:
                print(f"  {bot_label}: already patched")
                return False
            cb_idx = code.find("const contextBlock")
            if cb_idx == -1:
                print(f"  {bot_label}: contextBlock not found")
                return False
            close_idx = code[:cb_idx].rfind('";')
            if close_idx == -1:
                print(f"  {bot_label}: close marker not found")
                return False
            escaped = addition.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            code = code[:close_idx] + escaped + code[close_idx:]
            node["parameters"]["jsCode"] = code
            print(f"  {bot_label}: patched")
            return True
    return False

patched = add_to_prompt(cos_d["nodes"], "COS", SECURITY_TIERS_ADDITION, "🟢 SAFE")
if patched:
    with open(cos_path, "w", encoding="utf-8") as f:
        json.dump(cos_d, f, ensure_ascii=False, indent=2)
    print("  Saved bridge_chief_of_staff_bot.json")

# Also patch Security bot
sec_path = f"{N8N_DIR}/bridge_chief_sec_off_bot.json"
if os.path.exists(sec_path):
    with open(sec_path, encoding="utf-8") as f:
        sec_d = json.load(f)
    patched = add_to_prompt(sec_d["nodes"], "Security", SECURITY_TIERS_ADDITION, "🟢 SAFE")
    if patched:
        with open(sec_path, "w", encoding="utf-8") as f:
            json.dump(sec_d, f, ensure_ascii=False, indent=2)
        print("  Saved bridge_chief_sec_off_bot.json")

# Finance ROI patch
fin_path = f"{N8N_DIR}/bridge_finance_kpi_monitor.json"
if os.path.exists(fin_path):
    with open(fin_path, encoding="utf-8") as f:
        fin_d = json.load(f)
    patched = add_to_prompt(fin_d["nodes"], "Finance", FINANCE_ROI_ADDITION, "FINANCE ROI TRACKING")
    if patched:
        with open(fin_path, "w", encoding="utf-8") as f:
            json.dump(fin_d, f, ensure_ascii=False, indent=2)
        print("  Saved bridge_finance_kpi_monitor.json")
print()

# 4. Save new workflows
print("--- New workflows ---")
for name, wf in [
    ("bridge_self_healing.json", SELF_HEALING_WF),
    ("bridge_anomaly_detector.json", ANOMALY_DETECTOR_WF),
    ("bridge_cro_revenue_engine.json", CRO_REVENUE_ENGINE_WF),
]:
    out = f"{N8N_DIR}/{name}"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)
    print(f"  Saved {name}")
print()

# 5. Save schema SQL
with open(f"{SCHEMA_DIR}/bridge_v2_schema.sql", "w", encoding="utf-8") as f:
    f.write(V2_SCHEMA_SQL)
print("Saved n8n/schema/bridge_v2_schema.sql")

print("\n=== V2 Upgrade Done ===")
