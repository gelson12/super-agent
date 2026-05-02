"""
Bridge OS — CTO Integration Upgrade

1. Generates bridge_cto_bot.json from bridge_pm_bot.json as template
2. Patches COS, PM, CEO bots with CTO integration instructions
3. Creates SQL seed file for cto_bot_enabled
"""
import json
import copy
import os

N8N_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "n8n")
SCHEMA_DIR = os.path.join(N8N_DIR, "schema")
os.makedirs(SCHEMA_DIR, exist_ok=True)

# ─── CTO System Prompt ──────────────────────────────────────────────────────

CTO_SYSTEM_PROMPT = (
    "You are Bridge_CTO_BOT — Chief Technology Officer of Bridge Digital Solutions.\\n\\n"
    "ROLE\\n"
    "You are the technical brain of the organization. You own ALL technical decisions, architecture, "
    "and system efficiency. You sit in the DECISION LAYER and PRE-BUILD LAYER — you evaluate "
    "feasibility before anything is built.\\n\\n"
    "POSITION IN THE DECISION FLOW\\n"
    "BizDev/Research → CEO evaluates opportunity → COS orchestrates validation → "
    "[CRO + Finance + Security + CTO in parallel] → APPROVED/REJECTED\\n"
    "After approval: COS → PM → CTO defines architecture → PM assigns builders\\n\\n"
    "You receive memos of type 'cto_review_request' from COS or PM. You MUST respond with "
    "memo_type='cto_review_complete' containing your full technical assessment.\\n\\n"
    "PRIMARY RESPONSIBILITIES\\n"
    "1. TECHNICAL FEASIBILITY — Can this be built? What stack, APIs, tools. Scalability. Blockers.\\n"
    "2. ARCHITECTURE DESIGN — Define system structure, choose frameworks, design n8n workflows, "
    "GitHub deployments, container vs static decisions.\\n"
    "3. BUILD STRATEGY — Decide: template vs custom, Lovable vs GitHub, container vs static. "
    "Optimize speed and cost. Reuse before creating new.\\n"
    "4. COST OPTIMIZATION — Recommend model selection (Haiku for simple, Sonnet for complex). "
    "Minimize API calls. Estimate token budget.\\n"
    "5. RESOURCE MANAGEMENT — Control live site count, container usage. Coordinate with Finance.\\n"
    "6. PERFORMANCE & RELIABILITY — Ensure stability, detect inefficiencies, propose improvements.\\n\\n"
    "REQUIRED OUTPUT FOR cto_review_request:\\n"
    "Reply with memo_type='cto_review_complete' to the requesting agent (pm or chief_of_staff):\\n"
    "Provide in body_json: project_name, feasibility (high/medium/low), architecture_plan "
    "(stack, tools, deployment, estimated_build_time_days), cost_strategy "
    "(model_recommendation haiku/sonnet/mixed, estimated_token_budget, api_calls_per_day), "
    "efficiency_plan (reuse_opportunities, optimizations list), risks (technical list, scalability list), "
    "recommendation (PROCEED/MODIFY/REJECT), modification_notes if MODIFY or REJECT.\\n\\n"
    "FEASIBILITY SCORING:\\n"
    "- high: current stack, no new deps, < 5 days\\n"
    "- medium: new integration needed, 5-14 days, manageable risk\\n"
    "- low: significant unknowns, new infrastructure, > 14 days or high failure risk\\n\\n"
    "BEFORE EVERY EVALUATION:\\n"
    "1. Check bot_context for similar past projects and outcomes\\n"
    "2. Check if CRO already approved — tech plan must align with the revenue model\\n"
    "3. REUSE before creating new — check if existing infrastructure covers the need\\n\\n"
    "SCHEDULED REVIEWS (daily 10:30 UTC, weekly Monday 11:00 UTC):\\n"
    "- Scan for stalled builds (projects with cto_review_complete but no PM progress)\\n"
    "- Flag performance regressions or efficiency issues\\n"
    "- Report to COS on technical health\\n\\n"
    "AUTHORITY\\n"
    "You MAY: reject technically infeasible projects, recommend model/stack changes, define architecture, "
    "request COS override for blocked builds.\\n"
    "You MAY NOT: override CRO scoring, approve budget spend (Finance authority), "
    "deploy to production without PM + Security clearance.\\n\\n"
    "ESCALATE TO COS WHEN: architecture decision requires CEO input, budget exceeds Finance threshold, "
    "security risk discovered during technical review, build is stalled > 3 days.\\n\\n"
    "OUTPUT STYLE\\n"
    "Telegram replies: use \\\"⚙️ CTO REPORT\\\" header. Be technical but concise. "
    "Surface risks clearly. Never oversell feasibility.\\n\\n"
    "SUCCESS DEFINITION\\n"
    "You are successful when projects are built efficiently, with no overengineering, "
    "within budget, and with zero avoidable technical failures."
)

# ─── CTO context query ───────────────────────────────────────────────────────

CTO_CONTEXT_QUERY = (
    "SELECT jsonb_build_object(\n"
    "    'now_utc', NOW(),\n"
    "    'pending_reviews', (\n"
    "        SELECT COALESCE(jsonb_agg(row_to_json(m)), '[]'::jsonb)\n"
    "        FROM (\n"
    "            SELECT memo_id, from_agent, subject, body_json, created_at\n"
    "            FROM bridge.agent_memos\n"
    "            WHERE to_agent = 'cto'\n"
    "              AND memo_type IN ('cto_review_request','architecture_request','feasibility_request')\n"
    "              AND status = 'open'\n"
    "            ORDER BY created_at DESC LIMIT 10\n"
    "        ) m\n"
    "    ),\n"
    "    'recent_cro_approvals', (\n"
    "        SELECT COALESCE(jsonb_agg(row_to_json(c)), '[]'::jsonb)\n"
    "        FROM (\n"
    "            SELECT project_name, cro_score, recommendation, created_at\n"
    "            FROM bridge.cro_evaluations\n"
    "            WHERE recommendation = 'APPROVE' AND created_at >= NOW() - INTERVAL '7d'\n"
    "            ORDER BY created_at DESC LIMIT 5\n"
    "        ) c\n"
    "    ),\n"
    "    'project_memory_recent', (\n"
    "        SELECT COALESCE(jsonb_agg(row_to_json(pm)), '[]'::jsonb)\n"
    "        FROM (\n"
    "            SELECT project_name, outcome, cro_score, key_insights, updated_at\n"
    "            FROM bridge.project_memory\n"
    "            WHERE updated_at >= NOW() - INTERVAL '30d'\n"
    "            ORDER BY updated_at DESC LIMIT 8\n"
    "        ) pm\n"
    "    ),\n"
    "    'stalled_builds', (\n"
    "        SELECT COALESCE(jsonb_agg(row_to_json(s)), '[]'::jsonb)\n"
    "        FROM (\n"
    "            SELECT memo_id, from_agent, subject, created_at\n"
    "            FROM bridge.agent_memos\n"
    "            WHERE to_agent = 'cto'\n"
    "              AND status = 'open'\n"
    "              AND created_at < NOW() - INTERVAL '3 days'\n"
    "            ORDER BY created_at ASC LIMIT 5\n"
    "        ) s\n"
    "    )\n"
    ") AS context;"
)

# ─── Patches for existing bots ───────────────────────────────────────────────

COS_CTO_ADDITION = (
    "\n\nCTO INTEGRATION (mandatory):\n"
    "The CTO is the technical authority. ALL projects requiring any build, deployment, or "
    "technical implementation MUST receive CTO review before PM execution.\n\n"
    "UPDATED DECISION VALIDATION ORDER (all 4 must pass before CEO approval):\n"
    "1. CRO → commercial viability (score >= 70)\n"
    "2. Finance → budget available\n"
    "3. Security → risk acceptable\n"
    "4. CTO → technically feasible (recommendation = PROCEED)\n\n"
    "HOW TO TRIGGER CTO REVIEW:\n"
    "Send memo_type='cto_review_request' to_agent='cto' with body_json containing:\n"
    "  project_name, project_summary, cro_score, expected_roi_usd, timeline_weeks, "
    "any known technical requirements.\n"
    "Wait for cto_review_complete before forwarding to PM.\n\n"
    "ROUTING RULES BASED ON CTO RESPONSE:\n"
    "- recommendation = PROCEED → forward cto_review_complete to PM with architecture plan\n"
    "- recommendation = MODIFY → send revision notes to BizDev/Researcher, loop back\n"
    "- recommendation = REJECT → notify CEO with technical rejection reason, kill request\n\n"
    "FALLBACK VOTING (5-day owner timeout):\n"
    "If owner approval not received within 5 days, trigger a 5-agent vote: "
    "CRO + Finance + Security + CTO + COS.\n"
    "Rules: minimum 4/5 votes to proceed, Security has veto power, high-risk actions "
    "NEVER auto-approved regardless of votes.\n"
    "Log vote outcome to bridge.workflow_events before proceeding."
)

PM_CTO_ADDITION = (
    "\n\nCTO ARCHITECTURE GATE (mandatory):\n"
    "You are the execution manager. The CTO designs architecture; you execute it.\n"
    "NEVER assign any build task to Website Bot or Programmer Bot without a CTO architecture plan.\n\n"
    "BEFORE every build assignment:\n"
    "1. Check your open_inbox for a memo of memo_type='cto_review_complete' for this project.\n"
    "2. If NOT found → send memo_type='cto_review_request' to_agent='cto' with project details. "
    "Do NOT assign to builders until cto_review_complete arrives.\n"
    "3. If FOUND → extract architecture_plan from body_json and use it:\n"
    "   - Use cost_strategy.model_recommendation for all build assignments\n"
    "   - Use architecture_plan.stack and tools in your task briefs\n"
    "   - Use architecture_plan.estimated_build_time_days for milestones\n"
    "   - If recommendation = REJECT → escalate to COS immediately, do NOT build\n"
    "   - If recommendation = MODIFY → revise brief and re-request CTO review\n\n"
    "POST-BUILD VALIDATION:\n"
    "After build completes, send memo_type='build_validation_request' to_agent='cto' "
    "with deploy URL and build output. CTO validates before you mark the milestone complete.\n"
    "You do NOT have authority to declare a build successful without CTO sign-off."
)

CEO_CTO_ADDITION = (
    "\n\nCTO AWARENESS:\n"
    "The CTO bot owns all technical decisions and architecture. Nothing gets built without CTO approval.\n\n"
    "WHEN RECEIVING PROJECT PROPOSALS:\n"
    "1. Verify cto_review_complete exists in body_json (alongside cro_score)\n"
    "2. If missing → ask COS to trigger CTO review before you approve\n"
    "3. CTO recommendation = REJECT overrides your approval authority on technical grounds\n\n"
    "FINAL APPROVAL SCORING:\n"
    "  CRO score (40%) + CTO feasibility (20%) + Finance (20%) + Security (20%) = approval decision\n"
    "  Minimum combined threshold: 70 points to approve\n\n"
    "TECHNICAL VETO:\n"
    "If CTO flags a fundamental technical risk (infrastructure failure, security hole, "
    "unresolvable dependency), you MUST respect the veto even if CRO score is high.\n"
    "Document veto reason in bridge.project_memory for future reference."
)

# ─── Fallback voting workflow ─────────────────────────────────────────────────

FALLBACK_VOTING_WORKFLOW = {
    "name": "Bridge_Fallback_Voting_System",
    "nodes": [
        {
            "id": "fv-trigger",
            "name": "Schedule: every 6h",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [240, 300],
            "parameters": {
                "rule": {
                    "interval": [{"field": "cronExpression", "expression": "0 0 */6 * * *"}]
                }
            }
        },
        {
            "id": "fv-check",
            "name": "Find timed-out approvals",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 300],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT memo_id, from_agent, subject, body_json, created_at,\n"
                    "       NOW() - created_at AS age,\n"
                    "       EXTRACT(EPOCH FROM (NOW() - created_at))/86400 AS days_old\n"
                    "FROM bridge.agent_memos\n"
                    "WHERE memo_type IN ('approval_request', 'cro_review_request', 'cto_review_request')\n"
                    "  AND status = 'open'\n"
                    "  AND priority IN ('high', 'urgent')\n"
                    "  AND created_at < NOW() - INTERVAL '5 days'\n"
                    "  AND (body_json->>'vote_triggered') IS NULL\n"
                    "ORDER BY created_at ASC\n"
                    "LIMIT 5;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "fv-if-any",
            "name": "Any timed out?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.2,
            "position": [720, 300],
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "typeValidation": "strict"},
                    "conditions": [
                        {
                            "id": "has_rows",
                            "leftValue": "={{ $json.memo_id }}",
                            "rightValue": "",
                            "operator": {"type": "string", "operation": "notEmpty"}
                        }
                    ],
                    "combinator": "and"
                },
                "options": {}
            }
        },
        {
            "id": "fv-trigger-vote",
            "name": "Trigger vote memos",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [960, 200],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "WITH mark_voted AS (\n"
                    "  UPDATE bridge.agent_memos\n"
                    "  SET body_json = body_json || '{\"vote_triggered\": true}'::jsonb\n"
                    "  WHERE memo_id = $1::uuid\n"
                    "  RETURNING memo_id, subject, body_json\n"
                    "),\n"
                    "vote_request AS (\n"
                    "  INSERT INTO bridge.agent_memos\n"
                    "    (from_agent, to_agent, memo_type, priority, subject, body_json)\n"
                    "  VALUES\n"
                    "    ('chief_of_staff', 'cro',     'vote_request', 'urgent',\n"
                    "     $2, jsonb_build_object('original_memo_id', $1, 'vote_deadline_hours', 24)),\n"
                    "    ('chief_of_staff', 'finance',  'vote_request', 'urgent',\n"
                    "     $2, jsonb_build_object('original_memo_id', $1, 'vote_deadline_hours', 24)),\n"
                    "    ('chief_of_staff', 'cso',      'vote_request', 'urgent',\n"
                    "     $2, jsonb_build_object('original_memo_id', $1, 'vote_deadline_hours', 24)),\n"
                    "    ('chief_of_staff', 'cto',      'vote_request', 'urgent',\n"
                    "     $2, jsonb_build_object('original_memo_id', $1, 'vote_deadline_hours', 24)),\n"
                    "    ('chief_of_staff', 'chief_of_staff', 'vote_request', 'urgent',\n"
                    "     $2, jsonb_build_object('original_memo_id', $1, 'vote_deadline_hours', 24))\n"
                    "  RETURNING memo_id\n"
                    "),\n"
                    "log_event AS (\n"
                    "  INSERT INTO bridge.workflow_events (workflow_name, event_type, details_json)\n"
                    "  VALUES ('fallback_voting', 'vote_triggered',\n"
                    "    jsonb_build_object('original_memo_id', $1, 'subject', $2))\n"
                    "  RETURNING event_id\n"
                    ")\n"
                    "SELECT $1 AS original_memo_id, COUNT(*) AS vote_memos_created\n"
                    "FROM vote_request;"
                ),
                "options": {
                    "queryReplacement": "={{ $json.memo_id }},={{ $json.subject }}"
                }
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "fv-tally",
            "name": "Tally completed votes",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [480, 500],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "SELECT\n"
                    "  v.original_memo_id,\n"
                    "  o.subject,\n"
                    "  COUNT(v.memo_id) AS total_votes,\n"
                    "  COUNT(CASE WHEN (v.body_json->>'vote') = 'APPROVE' THEN 1 END) AS approve_votes,\n"
                    "  COUNT(CASE WHEN (v.body_json->>'vote') = 'REJECT' THEN 1 END) AS reject_votes,\n"
                    "  MAX(CASE WHEN v.from_agent = 'cso' AND (v.body_json->>'vote') = 'REJECT'\n"
                    "      THEN 1 ELSE 0 END) AS security_veto,\n"
                    "  (o.body_json->>'high_risk')::boolean AS is_high_risk\n"
                    "FROM bridge.agent_memos v\n"
                    "JOIN bridge.agent_memos o ON o.memo_id = (v.body_json->>'original_memo_id')::uuid\n"
                    "WHERE v.memo_type = 'vote_request'\n"
                    "  AND v.status = 'resolved'\n"
                    "  AND (o.body_json->>'vote_triggered') = 'true'\n"
                    "  AND o.status = 'open'\n"
                    "GROUP BY v.original_memo_id, o.subject, o.body_json\n"
                    "HAVING COUNT(v.memo_id) >= 4\n"
                    "   AND COUNT(CASE WHEN v.status = 'resolved' THEN 1 END) >= 4;"
                ),
                "options": {}
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        },
        {
            "id": "fv-decide",
            "name": "Apply vote decision",
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [720, 500],
            "parameters": {
                "operation": "executeQuery",
                "query": (
                    "WITH decision AS (\n"
                    "  SELECT\n"
                    "    $1::uuid AS memo_id,\n"
                    "    CASE\n"
                    "      WHEN $6::boolean = true THEN 'REJECTED'  -- high-risk never auto-approved\n"
                    "      WHEN $4::int = 1 THEN 'REJECTED'          -- security veto\n"
                    "      WHEN $2::int >= 4 THEN 'APPROVED'\n"
                    "      ELSE 'REJECTED'\n"
                    "    END AS outcome,\n"
                    "    $2::int AS approve_votes,\n"
                    "    $3::int AS reject_votes,\n"
                    "    $4::int AS security_veto\n"
                    "),\n"
                    "update_memo AS (\n"
                    "  UPDATE bridge.agent_memos\n"
                    "  SET status = CASE WHEN (SELECT outcome FROM decision) = 'APPROVED'\n"
                    "                   THEN 'resolved' ELSE 'resolved' END,\n"
                    "      resolved_at = NOW(),\n"
                    "      resolved_by = 'fallback_voting',\n"
                    "      resolution_notes = (SELECT outcome FROM decision) || ' by fallback vote (' ||\n"
                    "        (SELECT approve_votes::text FROM decision) || '/5 approve, security_veto=' ||\n"
                    "        (SELECT security_veto::text FROM decision) || ')'\n"
                    "  WHERE memo_id = $1::uuid\n"
                    "  RETURNING memo_id\n"
                    "),\n"
                    "notify_cos AS (\n"
                    "  INSERT INTO bridge.agent_memos\n"
                    "    (from_agent, to_agent, memo_type, priority, subject, body_json)\n"
                    "  VALUES (\n"
                    "    'fallback_voting', 'chief_of_staff', 'vote_result', 'high',\n"
                    "    'Fallback vote result: ' || (SELECT outcome FROM decision),\n"
                    "    jsonb_build_object(\n"
                    "      'original_memo_id', $1,\n"
                    "      'outcome', (SELECT outcome FROM decision),\n"
                    "      'approve_votes', $2, 'reject_votes', $3,\n"
                    "      'security_veto', $4, 'is_high_risk', $6\n"
                    "    )\n"
                    "  ) RETURNING memo_id\n"
                    "),\n"
                    "log_event AS (\n"
                    "  INSERT INTO bridge.workflow_events (workflow_name, event_type, details_json)\n"
                    "  VALUES ('fallback_voting', 'vote_decided',\n"
                    "    jsonb_build_object('memo_id', $1, 'outcome', (SELECT outcome FROM decision),\n"
                    "      'votes', $2 || '/5'))\n"
                    "  RETURNING event_id\n"
                    ")\n"
                    "SELECT (SELECT outcome FROM decision) AS final_outcome,\n"
                    "       (SELECT memo_id FROM notify_cos) AS cos_notified;"
                ),
                "options": {
                    "queryReplacement": (
                        "={{ $json.original_memo_id }},"
                        "={{ $json.approve_votes }},"
                        "={{ $json.reject_votes }},"
                        "={{ $json.security_veto }},"
                        "={{ $json.subject }},"
                        "={{ $json.is_high_risk || false }}"
                    )
                }
            },
            "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}}
        }
    ],
    "connections": {
        "Schedule: every 6h": {"main": [[{"node": "Find timed-out approvals", "type": "main", "index": 0}]]},
        "Find timed-out approvals": {"main": [[{"node": "Any timed out?", "type": "main", "index": 0}, {"node": "Tally completed votes", "type": "main", "index": 0}]]},
        "Any timed out?": {"main": [[{"node": "Trigger vote memos", "type": "main", "index": 0}], []]},
        "Tally completed votes": {"main": [[{"node": "Apply vote decision", "type": "main", "index": 0}]]}
    },
    "settings": {"executionOrder": "v1"},
    "pinData": None
}

# ─── Helper: patch system prompt in Assemble prompt node ─────────────────────

def patch_system_prompt(nodes, bot_name, addition, unique_marker):
    """Append addition to the system string in the Assemble prompt node's jsCode."""
    for node in nodes:
        if node.get("name") == "Assemble prompt":
            code = node["parameters"]["jsCode"]
            if unique_marker in code:
                print(f"  {bot_name}: already patched, skipping")
                return False
            # Find the closing of the const system = "..." string.
            # It always appears as the last `";` before `const contextBlock`.
            cb_idx = code.find("const contextBlock")
            if cb_idx == -1:
                print(f"  {bot_name}: WARNING — const contextBlock not found")
                return False
            chunk = code[:cb_idx]
            close_idx = chunk.rfind('";')
            if close_idx == -1:
                print(f"  {bot_name}: WARNING — system string closing not found")
                return False
            # Escape the addition for JS string insertion
            escaped = (addition
                       .replace("\\", "\\\\")
                       .replace('"', '\\"')
                       .replace("\n", "\\n"))
            # Insert before the closing ";
            code = code[:close_idx] + escaped + code[close_idx:]
            node["parameters"]["jsCode"] = code
            print(f"  {bot_name}: system prompt patched")
            return True
    print(f"  {bot_name}: WARNING — Assemble prompt node not found")
    return False


# ─── Generate CTO bot from PM bot template ────────────────────────────────────

print("=== Generating bridge_cto_bot.json ===")

with open(f"{N8N_DIR}/bridge_pm_bot.json", encoding="utf-8") as f:
    pm = json.load(f)

cto = copy.deepcopy(pm)
cto["name"] = "bridge_cto_bot"
if "id" in cto:
    del cto["id"]
if "active" in cto:
    del cto["active"]

for node in cto["nodes"]:
    nid = node.get("id", "")
    name = node.get("name", "")

    # Update IDs from pm → cto
    node["id"] = nid.replace("_pm", "_cto").replace("pm_", "cto_")

    # Telegram trigger
    if name == "Telegram: user DM":
        node["disabled"] = True

    # Schedule: daily — 10:30 UTC
    elif name == "Schedule: daily":
        try:
            node["parameters"]["rule"]["interval"][0]["expression"] = "0 30 10 * * *"
        except (KeyError, IndexError):
            pass

    # Schedule: weekly — Monday 11:00 UTC
    elif name == "Schedule: weekly":
        try:
            node["parameters"]["rule"]["interval"][0]["expression"] = "0 0 11 * * 1"
        except (KeyError, IndexError):
            pass

    # Webhook path
    elif name == "Webhook: invoke":
        node["parameters"]["path"] = "bridge-cto-invoke"
        node["webhookId"] = "bridge-cto-invoke"

    # Read enabled flag
    elif name == "Read enabled flag":
        node["parameters"]["query"] = (
            "SELECT (value = 'true') AS enabled "
            "FROM bridge.system_limits WHERE key = 'cto_bot_enabled';"
        )

    # Fetch open inbox
    elif name == "Fetch open inbox":
        node["parameters"]["query"] = (
            "SELECT COALESCE(jsonb_agg(row_to_json(m) ORDER BY m.priority_rank, m.created_at), "
            "'[]'::jsonb) AS open_inbox\n"
            "FROM (\n"
            "  SELECT memo_id, from_agent, memo_type, priority, subject, body_json, created_at,\n"
            "         CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'normal' THEN 2 ELSE 3 END AS priority_rank\n"
            "  FROM bridge.agent_memos\n"
            "  WHERE to_agent IN ('cto', 'all') AND status = 'open'\n"
            "  LIMIT 20\n"
            ") m;"
        )

    # Fetch bot context — replace with CTO-specific context
    elif name == "Fetch bot context":
        node["parameters"]["query"] = CTO_CONTEXT_QUERY

    # Assemble prompt — patch system prompt
    elif name == "Assemble prompt":
        code = node["parameters"]["jsCode"]
        # Replace bot_name: "pm" with "cto"
        code = code.replace('bot_name: "pm"', 'bot_name: "cto"')
        # Replace daily/weekly spec
        code = code.replace(
            '"task_name": "daily_project_pulse"',
            '"task_name": "daily_tech_review"'
        )
        code = code.replace(
            '"task_name": "weekly_project_review"',
            '"task_name": "weekly_architecture_review"'
        )
        # Replace PM system prompt with CTO prompt
        # Find const system = "..."  (starts with You are Bridge_PM_BOT)
        pm_system_start = 'const system = "You are Bridge_PM_BOT'
        pm_system_end = 'SUCCESS DEFINITION\\nYou are successful when projects ship on time'
        # Find start
        s_idx = code.find(pm_system_start)
        if s_idx != -1:
            # Find the end of the system string (closing \n";)
            # Search from after the start
            end_marker = '\\n";\n\nconst contextBlock'
            e_idx = code.find(end_marker, s_idx)
            if e_idx == -1:
                end_marker = '";\n\nconst contextBlock'
                e_idx = code.find(end_marker, s_idx)
            if e_idx != -1:
                # Replace everything from start of system string to end marker
                escaped_cto = (CTO_SYSTEM_PROMPT
                               .replace("\\", "\\\\")
                               .replace('"', '\\"')
                               .replace("\n", "\\n"))
                new_system = f'const system = "{escaped_cto}\\n"'
                code = code[:s_idx] + new_system + end_marker + code[e_idx + len(end_marker):]
                print("  CTO bot: system prompt replaced in Assemble prompt")
            else:
                print("  CTO bot: WARNING — end marker not found for system prompt")
        else:
            print("  CTO bot: WARNING — PM system prompt start not found")

        node["parameters"]["jsCode"] = code

    # Reply on Telegram — update token env var
    elif name == "Reply on Telegram":
        jb = node["parameters"].get("jsonBody", "")
        jb = jb.replace(
            "BRIDGE_PM_BOT_TOKEN || $env.Bridge_CEO_BOT",
            "BRIDGE_CTO_BOT_TOKEN || $env.BRIDGE_CEO_BOT_TOKEN || $env.Bridge_CEO_BOT"
        )
        node["parameters"]["jsonBody"] = jb

    # Medium-risk: approval DM — update token
    elif name == "Medium-risk: approval DM":
        url = node["parameters"].get("url", "")
        url = url.replace(
            "BRIDGE_PM_BOT_TOKEN || $env.Bridge_CEO_BOT",
            "BRIDGE_CTO_BOT_TOKEN || $env.BRIDGE_CEO_BOT_TOKEN || $env.Bridge_CEO_BOT"
        )
        node["parameters"]["url"] = url

    # High-risk: escalate to CEO — change from_agent from 'pm' to 'cto'
    elif name == "High-risk: escalate to CEO":
        q = node["parameters"].get("query", "")
        q = q.replace("('pm'", "('cto'")
        node["parameters"]["query"] = q

    # Medium-risk: log pending — change from 'pm' to 'cto'
    elif name == "Medium-risk: log pending":
        qr = node["parameters"].get("options", {}).get("queryReplacement", "")
        qr = qr.replace("$json.bot_name", "$json.bot_name")  # already dynamic via $2
        node["parameters"]["options"]["queryReplacement"] = qr

    # High-risk: urgent DM — update token
    elif name == "High-risk: urgent DM":
        url = node["parameters"].get("url", "")
        url = url.replace(
            "BRIDGE_PM_BOT_TOKEN || $env.Bridge_CEO_BOT",
            "BRIDGE_CTO_BOT_TOKEN || $env.BRIDGE_CEO_BOT_TOKEN || $env.Bridge_CEO_BOT"
        )
        node["parameters"]["url"] = url

with open(f"{N8N_DIR}/bridge_cto_bot.json", "w", encoding="utf-8") as f:
    json.dump(cto, f, ensure_ascii=False, indent=2)
print(f"Saved bridge_cto_bot.json\n")


# ─── Patch COS, PM, CEO bots ─────────────────────────────────────────────────

patches = [
    ("bridge_chief_of_staff_bot.json", "Chief of Staff", COS_CTO_ADDITION, "CTO INTEGRATION (mandatory)"),
    ("bridge_pm_bot.json",             "PM",             PM_CTO_ADDITION,  "CTO ARCHITECTURE GATE (mandatory)"),
    ("bridge_ceo_bot.json",            "CEO",            CEO_CTO_ADDITION, "CTO AWARENESS"),
]

for filename, label, addition, marker in patches:
    print(f"=== Patching {filename} ===")
    fpath = f"{N8N_DIR}/{filename}"
    with open(fpath, encoding="utf-8") as f:
        d = json.load(f)
    patched = patch_system_prompt(d["nodes"], label, addition, marker)
    if patched:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"  Saved {filename}")
    print()


# ─── Fallback voting workflow ─────────────────────────────────────────────────

print("=== Saving bridge_fallback_voting.json ===")
with open(f"{N8N_DIR}/bridge_fallback_voting.json", "w", encoding="utf-8") as f:
    json.dump(FALLBACK_VOTING_WORKFLOW, f, ensure_ascii=False, indent=2)
print("Saved bridge_fallback_voting.json\n")


# ─── SQL seed file ────────────────────────────────────────────────────────────

sql = """\
-- CTO bot enable flag + vote_results table support
INSERT INTO bridge.system_limits (key, value)
VALUES ('cto_bot_enabled', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true';

-- Ensure vote_request memo_type is handled by existing indexes
-- (no schema change needed — memo_type is free-form text)

-- Seed initial CTO authority level
INSERT INTO bridge.agent_performance (agent_name, date, authority_level)
VALUES ('cto', CURRENT_DATE, 9)
ON CONFLICT (agent_name, date) DO UPDATE SET authority_level = 9;
"""

with open(f"{SCHEMA_DIR}/seed_cto_enabled.sql", "w", encoding="utf-8") as f:
    f.write(sql)
print("Saved n8n/schema/seed_cto_enabled.sql")

print("\n=== Done ===")
print("Files created/modified:")
print("  n8n/bridge_cto_bot.json")
print("  n8n/bridge_chief_of_staff_bot.json")
print("  n8n/bridge_pm_bot.json")
print("  n8n/bridge_ceo_bot.json")
print("  n8n/bridge_fallback_voting.json")
print("  n8n/schema/seed_cto_enabled.sql")
