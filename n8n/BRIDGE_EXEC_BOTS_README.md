# Bridge Executive Bots — Deployment Guide

5 supervisory super-agents that sit above the operational pipeline (Researcher
B / Website C / Marketing D / PM A / Billing E). Each is one n8n workflow with
3 entry points (Telegram DM, scheduled cron, inter-agent webhook) and a shared
"think → act → memo → respond" subgraph. Plan: `~/.claude/plans/proceed-calm-ember.md`.

| Bot | Workflow file | Telegram env var | Model |
|---|---|---|---|
| Bridge_Researcher_bot      | `bridge_researcher_bot.json`      | `BRIDGE_RESEARCHER_BOT_TOKEN`      | CLAUDE |
| Bridge_Chief_Of_Staff_bot  | `bridge_chief_of_staff_bot.json`  | `BRIDGE_CHIEF_OF_STAFF_BOT_TOKEN`  | CLAUDE |
| Bridge_Chief_Sec_Off_bot   | `bridge_chief_sec_off_bot.json`   | `BRIDGE_CHIEF_SEC_OFF_BOT_TOKEN`   | CLAUDE |
| Bridge_CEO_BOT             | `bridge_ceo_bot.json`             | `BRIDGE_CEO_BOT_TOKEN`             | CLAUDE |
| Bridge_Cleaner_bot         | `bridge_cleaner_bot.json`         | `BRIDGE_CLEANER_BOT_TOKEN`         | GEMINI |

## 1. Prerequisites

Already set on Railway `N8N` service (confirmed during planning):
- `BRIDGE_ADMIN_TELEGRAM_CHAT_ID`
- `SUPER_AGENT_PASSWORD`
- `MEMORY_INGEST_SECRET`
- The 5 `BRIDGE_*_BOT_TOKEN` values

Add one more env var (used for inter-agent webhook auth):
```
BRIDGE_WEBHOOK_TOKEN=<32-char hex>
```

User must `/start` each of the 5 bots once in Telegram before that bot can DM
them — otherwise `sendMessage` returns *"bot can't initiate conversation"*.

## 2. Apply the schema migration

Adds `bridge.agent_memos` (the inter-agent queue), 5 enabled-flag knobs, and
two convenience views.

```bash
psql "$DATABASE_URL" -f n8n/bridge_agent_memos_schema.sql
```

Or run via n8n's Postgres node manually.

## 3. Generate the workflow JSON files

```bash
cd super-agent
python scripts/bridge_exec_build_chief_of_staff_bot.py
python scripts/bridge_exec_build_researcher_bot.py
python scripts/bridge_exec_build_ceo_bot.py
python scripts/bridge_exec_build_chief_sec_off_bot.py
python scripts/bridge_exec_build_cleaner_bot.py
```

Each prints `[OK] Wrote n8n/<bot>.json (<size> bytes, <N> nodes)`.

## 4. Import into n8n + create Telegram credentials

For each of the 5 generated JSONs:

1. **n8n UI → Workflows → Import from File** → pick the JSON.
2. Open the workflow. The "Telegram: user DM" node has an unbound credential
   slot named (e.g.) `Bridge Chief of Staff Bot`. Click → **Create new
   credential** → paste the matching `BRIDGE_*_BOT_TOKEN` value. Save.
3. Activate the workflow.

The webhook trigger registers automatically at:
`https://outstanding-blessing-production-1d4b.up.railway.app/webhook/bridge-<bot>-invoke`

The Schedule triggers fire on UTC; Railway runs UTC by default.

## 5. Verification

```sql
-- Sanity-check the schema migration
SELECT key, value FROM bridge.system_limits
 WHERE key LIKE '%_bot_enabled' ORDER BY key;
-- Should return 5 rows, all 'true'.
```

**Conversational test** — DM `Bridge_CEO_Bot` something like *"what's the top
priority this week?"* → expect a reply within 30s grounded in current
`campaign_targets` + `agent_memos`.

**Inter-agent memo test:**
```sql
INSERT INTO bridge.agent_memos
  (from_agent, to_agent, memo_type, priority, subject, body_json)
VALUES
  ('user', 'researcher', 'question', 'high',
   'test: any plumber niches in London worth scaling?', '{}'::jsonb);
```
Next 07:30 UTC Researcher tick (or the next manual webhook fire) will pick it
up and respond via memo + Telegram DM.

**Scheduled-cadence test** — temporarily drop one Schedule cron to every 5 min
in the Chief of Staff workflow → confirm a Telegram briefing arrives.

**Escalation test:**
```sql
INSERT INTO bridge.agent_memos
  (from_agent, to_agent, memo_type, priority, subject, body_json)
VALUES
  ('cso', 'ceo', 'escalation', 'urgent',
   'fake exposure test', '{"severity":"high"}'::jsonb);
```
Hit the CEO webhook to force-process: `curl -X POST .../webhook/bridge-ceo-invoke?t=$BRIDGE_WEBHOOK_TOKEN -d '{"task":"escalation_check"}'`.

## 6. Cadence schedule (UTC)

| Time | Bot | Task |
|---|---|---|
| 07:30 daily | Researcher | morning intelligence cycle |
| 08:00 daily | Chief of Staff | day-open alignment |
| 08:30 daily | CSO | daily risk review |
| 12:30 daily | Chief of Staff | midday blocker review |
| 16:00 daily | CEO | exec summary |
| 20:00 daily | Chief of Staff | day-close reconciliation |
| 22:00 daily | Cleaner | nightly cleanup (12h-TTL demos, 14d-expired memos) |
| Mon 09:00 | CEO | weekly priority alignment |
| Tue 10:00 | Researcher | weekly opportunity deep-dive |
| Wed 10:00 | CSO | weekly controls review |
| Thu 10:00 | Chief of Staff | weekly profit review |
| Fri 18:00 | Chief of Staff + Cleaner | weekly strategic summary + archive cycle |

## 7. Pause individual bots

```sql
UPDATE bridge.system_limits SET value = 'false'
 WHERE key = 'researcher_bot_enabled';
```
The "If enabled" gate inside each workflow short-circuits — no LLM cost, no
Telegram noise — until the flag flips back to `'true'`.

## 8. Autonomy model (graduated)

Every action returned by the LLM is risk-tagged before execution:

- **low** → auto-execute (memo insert, archive, no_op event log, escalate up).
- **medium** → Telegram approval DM to the user; not executed until APPROVE.
  Logged as an `approval_request` memo addressed to Chief of Staff.
- **high** → never executed. Inserts an urgent memo to CEO + sends an urgent
  Telegram DM. The user must execute manually. The workflow has no
  credentials capable of high-risk operations anyway.

Risk is determined by a static map in each bot's generator script — the LLM
cannot promote its own risk level. Update the map in the generator script and
regenerate the JSON to change behaviour.

## 9. Files in this layer

```
super-agent/n8n/bridge_agent_memos_schema.sql        — DDL + system_limits seeds + views
super-agent/n8n/bridge_<bot>.json                    — generated, 5 files
super-agent/scripts/_bridge_bot_skeleton.py          — shared workflow builder
super-agent/scripts/bridge_exec_build_<bot>.py       — 5 generator scripts
```

No changes required to super-agent app code. All needed endpoints
(`/chat/direct`, `/memory/ingest`, `/tools/github_delete_demo`) already exist.
