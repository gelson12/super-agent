# System Rollback Guide

## Last Known Good Backup
- **Date:** 2026-04-18
- **Git branch:** `backup/lkg-2026-04-18`
- **Git tag:** `lkg-2026-04-18`
- **Repo:** https://github.com/gelson12/super-agent
- **Contains:** 34 n8n workflow exports + full app code snapshot

---

## How to Roll Back n8n Workflows

### Restore a single workflow
```bash
# 1. Get the file from the backup branch
git show lkg-2026-04-18:n8n/live_backup/<WORKFLOW_FILE>.json > /tmp/restore.json

# 2. POST to n8n API to reimport
curl -X POST https://outstanding-blessing-production-1d4b.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_API_KEY" \
  -H "Content-Type: application/json" \
  -d @/tmp/restore.json
```

### Restore all workflows (nuclear rollback)
```bash
git clone https://github.com/gelson12/super-agent.git /tmp/restore-repo
cd /tmp/restore-repo
git checkout lkg-2026-04-18

for f in n8n/live_backup/*.json; do
  curl -s -X POST https://outstanding-blessing-production-1d4b.up.railway.app/api/v1/workflows \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H "Content-Type: application/json" \
    -d @"$f"
  echo "Imported: $f"
done
```

### Roll back the app code (super-agent container)
```bash
git checkout lkg-2026-04-18
git push origin lkg-2026-04-18:master --force  # triggers Railway redeploy
```

---

## Workflow File Index (n8n/live_backup/)

| File | Workflow Name | Active |
|---|---|---|
| `83ZQ9b5xReUaF6Ib__chief_of_staff_command_centre.json` | Chief of Staff — Command Centre | ✅ |
| `yuydLBM2TFJxeEEs__chief_of_staff_system_intelligence_operations.json` | Chief of Staff — System Intelligence & Operations | ✅ |
| `3k3lsAxKymNX7027__chief_of_staff_-_executive_monitor.json` | Chief of Staff — Executive Monitor | ✅ |
| `N4IBlfTKan8Oq4tQ__secretary_gmail_manager_gelsonmascarenhasgmailcom.json` | Secretary — Gmail Manager | ✅ |
| `nOawPhpTyNjPPiEb__secretary_outlook_email_calendar_operations.json` | Secretary — Outlook Email & Calendar | ✅ |
| `b43gK8dovLi2Tqzi__telegram_bot_-_claude_cli_chat.json` | Telegram Bot - Claude CLI Chat | ✅ |
| `7onbBjeUwHkSsuyc__crypto_specialist_super_agent_v3.json` | Crypto Specialist Super Agent v3 | ✅ |
| `CyiacnE5DIKGlmZz__senior_digital_content_intelligence_analyst.json` | Senior Digital Content Intelligence Analyst | ✅ |
| `jun8CaMnNhux1iEY__claude_verification_code_monitor.json` | Claude Verification Code Monitor | ✅ |
| `AWcSUH73xPxeCRg3__roll_call_daily_team_check_in.json` | Roll Call — Daily Team Check-In | ✅ |

> **Note on secrets:** Anthropic API key and Telegram bot tokens are redacted as `***REDACTED_***` in backup files.
> Re-enter them in n8n → Credentials after restoring, or hardcode temporarily in the workflow node.

---

## Infrastructure Reference

| Service | URL |
|---|---|
| n8n | https://outstanding-blessing-production-1d4b.up.railway.app |
| Super Agent | https://super-agent-production.up.railway.app |
| Inspiring Cat (CLI) | https://inspiring-cat-production.up.railway.app |
| Railway Project | divine-contentment |

---

## Known Issues Fixed in This Backup (do not revert past this point)
1. `Chief of Staff — System Intelligence` — gemini-2.5-flash → gemini-1.5-flash (Gemini free tier was 20 req/day, caused 429 death loop every 6h)
2. `Chief of Staff — Executive Monitor` — `fetch()` not available in n8n task runner; replaced with `this.helpers.httpRequest()`
3. Dashboard avatar — Chief of Staff workflows locked to `👨🏾` via name-based override in `static/index.html`
