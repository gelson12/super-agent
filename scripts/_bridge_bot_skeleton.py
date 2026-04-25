"""Shared builder for the 5 Bridge executive super-agent bot workflows.

Each bot workflow has the same shape:

    [Telegram Trigger]  [Schedule Trigger (daily)]  [Schedule Trigger (weekly)]  [Webhook]
          |                    |                           |                        |
          └────────────────────┴───────────────────────────┴────────────────────────┘
                                     |
                    [Code: build task payload + read bot flag]
                                     |
                          [IF: bot enabled?]  -- reads bridge.system_limits
                                     |
                       [PG: fetch open inbox (agent_memos)]
                                     |
                          [PG: bot-specific context query]
                                     |
                 [Code: assemble /chat/direct message]
                                     |
                 [HTTP: POST super-agent /chat/direct]
                                     |
                 [Code: parse {reply_text, actions[]}
                         + map action.type -> risk]
                                     |
                          [Switch by risk level]
                       /             |              \\
                 low (auto)     medium (approve)     high (escalate only)
                     |                |                      |
        [Switch by action.type]  [Telegram APPROVE DM]  [Urgent memo + user DM]
         memo / alert / query
         archive / no_op
                     |
                [PG: insert memo / UPDATE]
                [HTTP: Telegram reply to user]
                [HTTP: /memory/ingest]
                [PG: bridge.workflow_events]

The factory functions below return n8n node dicts that are identical across
all 5 bots. Each bot's own generator script defines only:
  - BOT_NAME (e.g., 'chief_of_staff')
  - TELEGRAM_ENV_VAR (e.g., 'BRIDGE_CHIEF_OF_STAFF_BOT_TOKEN')
  - SYSTEM_PROMPT (verbatim from the user's brief)
  - DAILY_CRON (list of {hour, minute, task_name})
  - WEEKLY_CRON (list of {weekday, hour, minute, task_name})
  - LLM_MODEL ('CLAUDE' | 'GEMINI' | 'DEEPSEEK')
  - CONTEXT_QUERY (bot-specific SELECT feeding the LLM)
  - ACTION_RISK_MAP (dict mapping action.type -> 'low'|'medium'|'high')

Everything else is built here.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any

# n8n credential for the shared Railway Postgres (same one used by existing
# Bridge workflows — see memory/project_railway_infra.md).
PG_CRED = {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}

SUPER_AGENT_URL = "https://super-agent-production.up.railway.app"


# ───────────────────────── Triggers ─────────────────────────

def telegram_trigger(bot_name: str, cred_name: str) -> dict:
    """Telegram-trigger node — fires on any DM to the bot."""
    return {
        "parameters": {
            "updates": ["message"],
            "additionalFields": {},
        },
        "id": f"node_trigger_tg_{bot_name}",
        "name": "Telegram: user DM",
        "type": "n8n-nodes-base.telegramTrigger",
        "typeVersion": 1.1,
        "position": [240, 200],
        # Credential created in n8n UI with the bot's token. Name is the
        # stable key; ID is rebound on import.
        "credentials": {"telegramApi": {"id": "", "name": cred_name}},
    }


def schedule_trigger_daily(cron_spec: list[dict], node_id: str = "node_trigger_daily") -> dict:
    """Daily cron trigger. cron_spec = [{'hour': H, 'minute': M, 'task_name': str}, ...]."""
    rules = []
    for item in cron_spec:
        rules.append({
            "field": "cronExpression",
            "expression": f"0 {item['minute']} {item['hour']} * * *",
        })
    return {
        "parameters": {"rule": {"interval": rules}},
        "id": node_id,
        "name": "Schedule: daily",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [240, 400],
    }


def schedule_trigger_weekly(cron_spec: list[dict], node_id: str = "node_trigger_weekly") -> dict:
    """Weekly cron trigger. cron_spec = [{'weekday': 0-6, 'hour':H, 'minute':M, 'task_name':str}]."""
    rules = []
    for item in cron_spec:
        rules.append({
            "field": "cronExpression",
            "expression": f"0 {item['minute']} {item['hour']} * * {item['weekday']}",
        })
    return {
        "parameters": {"rule": {"interval": rules}},
        "id": node_id,
        "name": "Schedule: weekly",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [240, 600],
    }


def webhook_trigger(bot_name: str) -> dict:
    """Inter-agent webhook — other workflows/users can POST here to invoke this bot."""
    return {
        "parameters": {
            "httpMethod": "POST",
            "path": f"bridge-{bot_name.replace('_','-')}-invoke",
            "responseMode": "responseNode",
            "options": {},
        },
        "id": f"node_trigger_webhook_{bot_name}",
        "name": "Webhook: invoke",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [240, 800],
        "webhookId": f"bridge-{bot_name.replace('_','-')}-invoke",
    }


# ───────────────────────── Build payload ─────────────────────────

def code_build_task(bot_name: str, daily_spec: list[dict], weekly_spec: list[dict]) -> dict:
    """Normalises all 4 trigger inputs into a uniform {task, user_text?, chat_id?} payload."""
    # Dense JS inline — n8n Code nodes run JavaScript.
    js = """
const src = $input.all()[0];
const nodeName = $execution.mode === 'manual' ? 'manual' : Object.keys($("Telegram: user DM").all ? {} : {})[0] || '';

// Detect trigger source by inspecting the item shape.
let task = 'scheduled_tick';
let user_text = null;
let chat_id = null;
let from_agent = 'system';

if (src.json && src.json.message && src.json.message.chat) {
    // Telegram trigger
    task = 'user_dm';
    user_text = src.json.message.text || '';
    chat_id = src.json.message.chat.id;
    from_agent = 'user';
} else if (src.json && src.json.body && src.json.body.task) {
    // Webhook trigger
    task = src.json.body.task;
    user_text = src.json.body.message || null;
    from_agent = src.json.body.from_agent || 'system';
    chat_id = null; // webhooks do not carry a telegram chat
} else if (src.json && src.json.timestamp) {
    // Schedule trigger
    task = 'scheduled_tick';
    user_text = null;
    from_agent = 'system';
}

return [{ json: {
    bot_name: BOT_NAME_PLACEHOLDER,
    task,
    user_text,
    chat_id,
    from_agent,
    now_iso: new Date().toISOString(),
    daily_spec: DAILY_SPEC_PLACEHOLDER,
    weekly_spec: WEEKLY_SPEC_PLACEHOLDER,
} }];
""".strip()
    js = (
        js.replace("BOT_NAME_PLACEHOLDER", json.dumps(bot_name))
          .replace("DAILY_SPEC_PLACEHOLDER", json.dumps(daily_spec))
          .replace("WEEKLY_SPEC_PLACEHOLDER", json.dumps(weekly_spec))
    )
    return {
        "parameters": {"jsCode": js},
        "id": "node_build_task",
        "name": "Build task payload",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [460, 400],
    }


# ───────────────────────── Enabled-flag gate ─────────────────────────

def pg_read_enabled_flag(bot_name: str) -> dict:
    """Reads bridge.system_limits for the bot's enabled flag."""
    query = (
        "SELECT (value = 'true') AS enabled "
        "FROM bridge.system_limits "
        f"WHERE key = '{bot_name}_bot_enabled';"
    )
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": query,
            "options": {},
        },
        "id": "node_read_enabled",
        "name": "Read enabled flag",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [680, 400],
        "credentials": {"postgres": PG_CRED},
    }


def if_enabled() -> dict:
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                "conditions": [{
                    "id": "enabled_check",
                    "leftValue": "={{ $json.enabled }}",
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": "node_if_enabled",
        "name": "If enabled",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [900, 400],
    }


# ───────────────────────── Context gather ─────────────────────────

def pg_fetch_inbox(bot_name: str) -> dict:
    """Reads this bot's open-memo inbox (addressed to it or broadcast)."""
    query = (
        "SELECT memo_id, from_agent, memo_type, priority, subject, body_json, "
        "       created_at, related_lead_id "
        "FROM bridge.agent_memos "
        f"WHERE to_agent IN ('{bot_name}', 'all') "
        "  AND status = 'open' "
        "ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
        "                       WHEN 'normal' THEN 2 ELSE 3 END, created_at "
        "LIMIT 20;"
    )
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": query,
            "options": {},
        },
        "id": "node_fetch_inbox",
        "name": "Fetch open inbox",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [1120, 400],
        "credentials": {"postgres": PG_CRED},
    }


def pg_fetch_context(context_query: str, node_name: str = "Fetch bot context") -> dict:
    """Bot-specific context SELECT (supplied by the per-bot builder)."""
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": context_query,
            "options": {},
        },
        "id": "node_fetch_context",
        "name": node_name,
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [1120, 600],
        "credentials": {"postgres": PG_CRED},
    }


# ───────────────────────── Assemble prompt + call LLM ─────────────────────────

def code_assemble_prompt(system_prompt: str) -> dict:
    """Concatenates system prompt + context + task for /chat/direct."""
    js = """
const task = $('Build task payload').first().json;
const inbox = $('Fetch open inbox').all().map(i => i.json);
const ctx = $('Fetch bot context').all().map(i => i.json);

const system = SYSTEM_PROMPT_PLACEHOLDER;

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
      ? `This is a scheduled cadence run. Decide what to do given the current context, open memos, and your role.`
      : `An inter-agent invocation arrived from '${task.from_agent}': ${task.user_text || 'no message body'}. Decide how to respond.`;

const outputGuard = `\\n\\n[OUTPUT FORMAT]\\nReturn ONLY a JSON object (no prose, no markdown fences):\\n{\\n  "reply_text": "<what to send back via Telegram; empty string when a scheduled run should stay silent>",\\n  "actions": [\\n    {"type": "memo",     "payload": {"to_agent": "researcher|chief_of_staff|cso|ceo|cleaner|all", "memo_type": "status|proposal|directive|question|decision", "priority": "urgent|high|normal|low", "subject": "...", "body_json": {...}}},\\n    {"type": "archive",  "payload": {"memo_id": "<uuid-from-open-inbox>", "reason": "..."}},\\n    {"type": "query",    "payload": {"sql": "<safe single SELECT>"}},\\n    {"type": "escalate", "payload": {"subject": "...", "body_json": {...}}},\\n    {"type": "cleanup",  "payload": {"slug": "...", "reason": "..."}},\\n    {"type": "no_op",    "payload": {"reason": "..."}}\\n  ]\\n}\\nKeep actions <= 5. Never include non-whitelisted action types. User-facing commentary belongs in reply_text, not in alert actions.`;

const fullMessage = `${system}\\n\\n[CONTEXT]\\n${contextBlock}\\n\\n[TASK]\\n${taskBlock}${outputGuard}`;

return [{ json: {
    message: fullMessage,
    session_id: `bridge-${task.bot_name}-${task.now_iso.slice(0,16).replace(/[:T-]/g,'')}`,
    task_kind: task.task,
    user_chat_id: task.chat_id,
    bot_name: task.bot_name,
} }];
""".strip()
    # Embed prompt via JSON-encode so quotes / newlines survive.
    js = js.replace("SYSTEM_PROMPT_PLACEHOLDER", json.dumps(system_prompt))
    return {
        "parameters": {"jsCode": js},
        "id": "node_assemble_prompt",
        "name": "Assemble prompt",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1340, 400],
    }


def http_chat_direct(model: str) -> dict:
    """POST super-agent /chat/direct with the assembled prompt."""
    body = (
        '={ "message": ' + '{{ JSON.stringify($json.message) }}' + ', '
        '"model": "' + model + '", '
        '"session_id": ' + '{{ JSON.stringify($json.session_id) }}' + ' }'
    )
    return {
        "parameters": {
            "method": "POST",
            "url": f"{SUPER_AGENT_URL}/chat/direct",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Token", "value": "={{$env.SUPER_AGENT_PASSWORD}}"},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {"timeout": 60000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_chat_direct",
        "name": "super-agent /chat/direct",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [1560, 400],
    }


# ───────────────────────── Parse LLM response ─────────────────────────

def code_parse_response(action_risk_map: dict[str, str]) -> dict:
    """Extracts {reply_text, actions[]} and annotates each action with a risk level."""
    js = """
const raw = $json.response || '';
const defaultRisk = 'high'; // fail-closed: unknown actions treated as high-risk
const riskMap = ACTION_RISK_MAP_PLACEHOLDER;

let parsed;
try {
    // Strip code fences if the LLM wrapped the JSON.
    const cleaned = raw.trim().replace(/^```(?:json)?/, '').replace(/```$/, '').trim();
    parsed = JSON.parse(cleaned);
} catch (e) {
    // Safe fallback: no actions, just echo that parsing failed.
    parsed = { reply_text: 'I had trouble producing a structured response. Please rephrase or try again.', actions: [] };
}

const reply_text = typeof parsed.reply_text === 'string' ? parsed.reply_text : '';
const actions = Array.isArray(parsed.actions) ? parsed.actions.slice(0, 5) : [];

const annotated = actions.map(a => {
    const type = (a && typeof a.type === 'string') ? a.type : 'no_op';
    const risk = riskMap[type] || defaultRisk;
    return { type, risk, payload: a.payload || {} };
});

// Pull forward context we need downstream.
const upstream = $('Assemble prompt').first().json;

return [{ json: {
    reply_text,
    actions: annotated,
    bot_name: upstream.bot_name,
    user_chat_id: upstream.user_chat_id,
    task_kind: upstream.task_kind,
    model_used: $json.model_used || 'unknown',
} }];
""".strip()
    js = js.replace("ACTION_RISK_MAP_PLACEHOLDER", json.dumps(action_risk_map))
    return {
        "parameters": {"jsCode": js},
        "id": "node_parse_response",
        "name": "Parse response + risk tag",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1780, 400],
    }


# ───────────────────────── Action dispatcher ─────────────────────────

def split_actions_to_items() -> dict:
    """Emits one n8n item per action so the downstream Switch can fan out."""
    js = """
const p = $json;
const out = [];
for (const a of (p.actions || [])) {
    out.push({ json: {
        action_type: a.type,
        action_risk: a.risk,
        action_payload: a.payload,
        bot_name: p.bot_name,
        user_chat_id: p.user_chat_id,
        task_kind: p.task_kind,
        model_used: p.model_used,
        reply_text: p.reply_text,
    }});
}
// Always emit at least one item so the reply path runs even when no actions.
if (out.length === 0) {
    out.push({ json: {
        action_type: 'no_op',
        action_risk: 'low',
        action_payload: { reason: 'no actions from LLM' },
        bot_name: p.bot_name,
        user_chat_id: p.user_chat_id,
        task_kind: p.task_kind,
        model_used: p.model_used,
        reply_text: p.reply_text,
    }});
}
return out;
""".strip()
    return {
        "parameters": {"jsCode": js},
        "id": "node_split_actions",
        "name": "Split actions",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [2000, 400],
    }


def switch_by_risk() -> dict:
    """Routes each action by risk level. Output 0 = low, 1 = medium, 2 = high."""
    return {
        "parameters": {
            "rules": {
                "values": [
                    {
                        "conditions": {
                            "options": {"caseSensitive": True, "typeValidation": "strict"},
                            "conditions": [{
                                "leftValue": "={{ $json.action_risk }}",
                                "rightValue": "low",
                                "operator": {"type": "string", "operation": "equals"},
                            }],
                            "combinator": "and",
                        },
                        "outputKey": "low",
                    },
                    {
                        "conditions": {
                            "options": {"caseSensitive": True, "typeValidation": "strict"},
                            "conditions": [{
                                "leftValue": "={{ $json.action_risk }}",
                                "rightValue": "medium",
                                "operator": {"type": "string", "operation": "equals"},
                            }],
                            "combinator": "and",
                        },
                        "outputKey": "medium",
                    },
                    {
                        "conditions": {
                            "options": {"caseSensitive": True, "typeValidation": "strict"},
                            "conditions": [{
                                "leftValue": "={{ $json.action_risk }}",
                                "rightValue": "high",
                                "operator": {"type": "string", "operation": "equals"},
                            }],
                            "combinator": "and",
                        },
                        "outputKey": "high",
                    },
                ],
            },
            "options": {"allMatchingOutputs": False, "fallbackOutput": 2},
        },
        "id": "node_switch_risk",
        "name": "Switch by risk",
        "type": "n8n-nodes-base.switch",
        "typeVersion": 3.2,
        "position": [2220, 400],
    }


# ───────────────────────── Action executors ─────────────────────────

def pg_execute_low_risk_action(bot_name: str) -> dict:
    """Runs low-risk actions: memo insert, archive, no_op event log.
    A single Postgres node dispatches by action_type via a CASE expression."""
    query = r"""
WITH input AS (
  SELECT $1::jsonb AS p
),
memo_insert AS (
  INSERT INTO bridge.agent_memos
    (from_agent, to_agent, memo_type, priority, subject, body_json, related_lead_id)
  SELECT
    $2,
    COALESCE(p->>'to_agent','all'),
    COALESCE(p->>'memo_type','status'),
    COALESCE(p->>'priority','normal'),
    COALESCE(p->>'subject','(no subject)'),
    COALESCE(p->'body_json','{}'::jsonb),
    NULLIF(p->>'related_lead_id','')::uuid
  FROM input
  WHERE $3 = 'memo'
  RETURNING memo_id
),
archive_memo AS (
  UPDATE bridge.agent_memos
  SET status = 'resolved', resolved_at = NOW(), resolved_by = $2,
      resolution_notes = COALESCE((SELECT p->>'reason' FROM input), 'archived by bot')
  WHERE $3 = 'archive'
    AND memo_id = (SELECT NULLIF((p->>'memo_id'),'')::uuid FROM input)
  RETURNING memo_id
),
event_log AS (
  INSERT INTO bridge.workflow_events
    (workflow_name, event_type, details_json)
  SELECT
    $2 || '_bot',
    CASE $3
      WHEN 'memo'    THEN 'memo_created'
      WHEN 'archive' THEN 'memo_archived'
      WHEN 'no_op'   THEN 'no_op'
      ELSE 'other_low_risk'
    END,
    (SELECT p FROM input)
  RETURNING event_id
)
SELECT
  (SELECT memo_id FROM memo_insert)  AS memo_created,
  (SELECT memo_id FROM archive_memo) AS memo_archived,
  (SELECT event_id FROM event_log)   AS event_logged;
"""
    qr = "={{ JSON.stringify($json.action_payload) }},{{ $json.bot_name }},{{ $json.action_type }}"
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": query,
            "options": {"queryReplacement": qr},
        },
        "id": "node_exec_low_risk",
        "name": "Execute low-risk action",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2440, 300],
        "credentials": {"postgres": PG_CRED},
    }


def http_medium_risk_approval(telegram_env_var: str) -> dict:
    """Medium-risk action: send approval-request DM to user; do NOT execute yet.
    User's APPROVE/REJECT reply is handled by Chief of Staff via the memo inbox."""
    # Text expression: NO leading '='. n8n evaluates {{ ... }} because the
    # outer jsonBody parameter is already '={...}'.
    text_expr = (
        "⚠️  *Approval request from {{$json.bot_name}}*\n"
        "\n"
        "  Action:  `{{$json.action_type}}`\n"
        "  Risk:    medium\n"
        "  Payload: {{ JSON.stringify($json.action_payload).slice(0,600) }}\n"
        "\n"
        "Reply *APPROVE {{$json.bot_name}} {{$json.action_type}}* to proceed, or ignore to reject.\n"
        "— Bridge Agents"
    )
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{$env." + telegram_env_var + "}}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "chat_id": {{$env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID}}, '
                '"text": ' + json.dumps(text_expr) + ', '
                '"parse_mode": "Markdown" }'
            ),
            "options": {"timeout": 15000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_medium_approval",
        "name": "Medium-risk: approval DM",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [2440, 500],
    }


def pg_medium_risk_memo() -> dict:
    """Log the pending approval as an open memo so Chief of Staff can track it."""
    query = (
        "INSERT INTO bridge.agent_memos "
        "(from_agent, to_agent, memo_type, priority, subject, body_json) "
        "VALUES ($1, 'chief_of_staff', 'approval_request', 'high', "
        "        'Pending medium-risk action awaiting user approval', $2::jsonb) "
        "RETURNING memo_id;"
    )
    qr = "={{ $json.bot_name }},{{ JSON.stringify({action_type: $json.action_type, action_payload: $json.action_payload}) }}"
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": query,
            "options": {"queryReplacement": qr},
        },
        "id": "node_medium_memo",
        "name": "Medium-risk: log pending",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2660, 500],
        "credentials": {"postgres": PG_CRED},
    }


def pg_high_risk_escalate(bot_name: str) -> dict:
    """High-risk action: NEVER auto-execute. Insert urgent memo to CEO + user."""
    # bot_name is known at build time, hard-coded into SQL to avoid comma-split issues.
    query = (
        "INSERT INTO bridge.agent_memos "
        "(from_agent, to_agent, memo_type, priority, subject, body_json) "
        f"VALUES ('{bot_name}', 'ceo', 'escalation', 'urgent', "
        "        'HIGH-RISK action requires manual execution', $1::jsonb) "
        "RETURNING memo_id;"
    )
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": query,
            "options": {"queryReplacement": "={{ JSON.stringify({action_type: $json.action_type, action_payload: $json.action_payload}) }}"},
        },
        "id": "node_high_escalate",
        "name": "High-risk: escalate to CEO",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2440, 700],
        "credentials": {"postgres": PG_CRED},
    }


def http_high_risk_alert(telegram_env_var: str) -> dict:
    """High-risk action: urgent Telegram DM."""
    text_expr = (
        "🚨  *HIGH-RISK action blocked (escalated to CEO)*\n"
        "\n"
        "  From:    {{$json.bot_name}}\n"
        "  Action:  `{{$json.action_type}}`\n"
        "  Payload: {{ JSON.stringify($json.action_payload).slice(0,600) }}\n"
        "\n"
        "This action was NOT executed. Review the agent_memos table for the escalation.\n"
        "— Bridge Agents"
    )
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{$env." + telegram_env_var + "}}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "chat_id": {{$env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID}}, '
                '"text": ' + json.dumps(text_expr) + ', '
                '"parse_mode": "Markdown" }'
            ),
            "options": {"timeout": 15000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_high_alert",
        "name": "High-risk: urgent DM",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [2660, 700],
    }


# ───────────────────────── Final reply to user ─────────────────────────

def http_telegram_reply(telegram_env_var: str) -> dict:
    """After all actions, send the LLM's reply_text back via Telegram.
    Note: chat_id can be a bare expression (numeric), but text must be a string
    literal with expressions embedded inside — otherwise JSON becomes invalid."""
    # String with {{ ... }} expressions interpolated by n8n at runtime.
    text_expr = "{{ $('Parse response + risk tag').first().json.reply_text || '(no reply)' }}"
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{$env." + telegram_env_var + "}}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "chat_id": '
                "{{ $('Parse response + risk tag').first().json.user_chat_id || $env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID }}"
                ", "
                '"text": ' + json.dumps(text_expr) + ' }'
            ),
            "options": {"timeout": 15000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_reply_tg",
        "name": "Reply on Telegram",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [2880, 400],
    }


def http_memory_ingest(bot_name: str) -> dict:
    """Post the round's decision to unified memory for cross-session recall."""
    body = (
        '={ "memories": [ { '
        '"content": ' + "{{ JSON.stringify("
        "($('Parse response + risk tag').first().json.reply_text || '(no reply)') + "
        "' [actions=' + (($('Parse response + risk tag').first().json.actions || []).map(a => a.type).join(',')) + ']'"
        ") }},"
        ' "memory_type": "decision",'
        f' "importance": 3,'
        f' "source": "bridge_{bot_name}_bot"'
        '}]}'
    )
    return {
        "parameters": {
            "method": "POST",
            "url": f"{SUPER_AGENT_URL}/memory/ingest",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Memory-Secret", "value": "={{$env.MEMORY_INGEST_SECRET}}"},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {"timeout": 15000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_memory_ingest",
        "name": "Memory ingest",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [3100, 400],
    }


def respond_webhook() -> dict:
    return {
        "parameters": {
            "respondWith": "json",
            "responseBody": '={"ok": true, "bot": {{JSON.stringify($("Parse response + risk tag").first().json.bot_name)}}}',
            "options": {},
        },
        "id": "node_respond",
        "name": "Respond OK",
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1,
        "position": [3320, 400],
    }


def if_has_reply() -> dict:
    """Gate the Telegram reply: fire only when the LLM produced non-empty text."""
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "typeValidation": "strict"},
                "conditions": [{
                    "id": "has_reply",
                    "leftValue": "={{ ($json.reply_text || '').trim().length }}",
                    "rightValue": 0,
                    "operator": {"type": "number", "operation": "gt"},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": "node_if_has_reply",
        "name": "If has reply",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [2000, 200],
    }


# ───────────────────────── Top-level assembly ─────────────────────────

def build_bot_workflow(
    *,
    bot_name: str,                       # e.g., 'chief_of_staff'
    workflow_display_name: str,          # e.g., 'bridge_chief_of_staff_bot'
    telegram_env_var: str,               # e.g., 'BRIDGE_CHIEF_OF_STAFF_BOT_TOKEN'
    telegram_cred_name: str,             # n8n credential name (UI-created)
    system_prompt: str,                  # verbatim brain prompt
    llm_model: str,                      # 'CLAUDE' | 'GEMINI' | 'DEEPSEEK'
    daily_cron: list[dict],              # [{'hour':H,'minute':M,'task_name':str}, ...]
    weekly_cron: list[dict],             # [{'weekday':0-6,'hour':H,'minute':M,'task_name':str}]
    context_query: str,                  # bot-specific SELECT feeding the LLM
    action_risk_map: dict[str, str],     # action.type -> risk level
) -> dict:
    """Assemble the full n8n workflow JSON for one executive bot."""
    nodes = [
        telegram_trigger(bot_name, telegram_cred_name),
        schedule_trigger_daily(daily_cron),
        schedule_trigger_weekly(weekly_cron),
        webhook_trigger(bot_name),

        code_build_task(bot_name, daily_cron, weekly_cron),

        pg_read_enabled_flag(bot_name),
        if_enabled(),

        pg_fetch_inbox(bot_name),
        pg_fetch_context(context_query),

        code_assemble_prompt(system_prompt),
        http_chat_direct(llm_model),
        code_parse_response(action_risk_map),

        # Per-task reply path (runs once).
        if_has_reply(),
        http_telegram_reply(telegram_env_var),
        http_memory_ingest(bot_name),

        # Per-action fan-out.
        split_actions_to_items(),
        switch_by_risk(),
        pg_execute_low_risk_action(bot_name),
        http_medium_risk_approval(telegram_env_var),
        pg_medium_risk_memo(),
        pg_high_risk_escalate(bot_name),
        http_high_risk_alert(telegram_env_var),

        respond_webhook(),
    ]

    # Wiring — all 4 triggers converge on "Build task payload".
    connections: dict[str, dict] = {
        "Telegram: user DM":       {"main": [[{"node": "Build task payload", "type": "main", "index": 0}]]},
        "Schedule: daily":         {"main": [[{"node": "Build task payload", "type": "main", "index": 0}]]},
        "Schedule: weekly":        {"main": [[{"node": "Build task payload", "type": "main", "index": 0}]]},
        "Webhook: invoke":         {"main": [[{"node": "Build task payload", "type": "main", "index": 0}]]},

        "Build task payload":      {"main": [[{"node": "Read enabled flag", "type": "main", "index": 0}]]},
        "Read enabled flag":       {"main": [[{"node": "If enabled",         "type": "main", "index": 0}]]},
        # IF output 0 = true branch → proceed. Output 1 = disabled → drop.
        "If enabled":              {"main": [
                                        [{"node": "Fetch open inbox", "type": "main", "index": 0}],
                                        [],
                                    ]},
        "Fetch open inbox":        {"main": [[{"node": "Fetch bot context",  "type": "main", "index": 0}]]},
        "Fetch bot context":       {"main": [[{"node": "Assemble prompt",    "type": "main", "index": 0}]]},
        "Assemble prompt":         {"main": [[{"node": "super-agent /chat/direct", "type": "main", "index": 0}]]},
        "super-agent /chat/direct":{"main": [[{"node": "Parse response + risk tag", "type": "main", "index": 0}]]},
        # Parse fans out to 3 parallel branches: reply gate, memory ingest, action fan-out.
        "Parse response + risk tag": {"main": [[
            {"node": "If has reply", "type": "main", "index": 0},
            {"node": "Memory ingest", "type": "main", "index": 0},
            {"node": "Split actions", "type": "main", "index": 0},
        ]]},

        # Reply gate: output 0 = has text → send; output 1 = empty → drop.
        "If has reply": {"main": [
            [{"node": "Reply on Telegram", "type": "main", "index": 0}],
            [],
        ]},
        "Reply on Telegram": {"main": [[{"node": "Respond OK", "type": "main", "index": 0}]]},
        "Memory ingest":     {"main": [[{"node": "Respond OK", "type": "main", "index": 0}]]},

        "Split actions":     {"main": [[{"node": "Switch by risk", "type": "main", "index": 0}]]},

        # Switch outputs: 0 = low → exec; 1 = medium → approval DM + memo; 2 = high → escalate + urgent DM.
        "Switch by risk": {"main": [
            [{"node": "Execute low-risk action", "type": "main", "index": 0}],
            [{"node": "Medium-risk: approval DM", "type": "main", "index": 0}],
            [{"node": "High-risk: escalate to CEO", "type": "main", "index": 0}],
        ]},
        "Execute low-risk action":    {"main": [[{"node": "Respond OK", "type": "main", "index": 0}]]},
        "Medium-risk: approval DM":   {"main": [[{"node": "Medium-risk: log pending", "type": "main", "index": 0}]]},
        "Medium-risk: log pending":   {"main": [[{"node": "Respond OK", "type": "main", "index": 0}]]},
        "High-risk: escalate to CEO": {"main": [[{"node": "High-risk: urgent DM", "type": "main", "index": 0}]]},
        "High-risk: urgent DM":       {"main": [[{"node": "Respond OK", "type": "main", "index": 0}]]},
    }

    return {
        "name": workflow_display_name,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
    }


def write_workflow(wf: dict, out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(wf, indent=2), encoding="utf-8")
    print(f"[OK]   Wrote {out_file} ({out_file.stat().st_size} bytes, {len(wf['nodes'])} nodes)")
