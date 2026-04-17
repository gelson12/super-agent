---
type: reference
tags: [n8n, patterns, api, workflows]
date: 2026-04-17
---

# n8n Patterns & API Reference

## REST API
- Base URL: `${N8N_BASE_URL}/api/v1`
- Auth header: `X-N8N-API-KEY: <key>`
- List workflows: `GET /workflows`
- Get workflow: `GET /workflows/{id}`
- Execute workflow: `POST /workflows/{id}/run`
- Create webhook: POST /webhooks, or use Webhook node inside workflow

## Common Workflow Patterns

### Schedule → Fetch → Notify
1. Cron/Schedule Trigger node (interval: every hour, daily, etc.)
2. HTTP Request node (GET external API)
3. IF node (condition check on response)
4. Email/Slack/Webhook Send node

### Webhook → Process → Respond
1. Webhook node (POST, auto-generate URL)
2. Set/Code node (transform payload)
3. Respond to Webhook node (200 OK + JSON body)

### Data Pipeline
1. Database/Spreadsheet node (source)
2. Loop Over Items node (batch processing)
3. HTTP Request node (per-item API calls)
4. Database/Spreadsheet node (destination)

## Node Quick Reference
- **HTTP Request**: any REST API call — set method, URL, auth, body
- **Code**: JavaScript snippets, `$input.all()` for all items
- **Set**: rename/add fields without code
- **IF/Switch**: branch logic
- **Merge**: combine two branches
- **Wait**: pause execution for N seconds
- **Send Email**: SMTP or Gmail OAuth credential
- **Slack**: send message to channel

## Error Patterns & Fixes
- `Cannot read properties of undefined`: missing field — check node input mapping
- `Request failed 401`: API key missing/expired — check credential store
- `Workflow not found`: ID changed or deleted — list workflows to confirm
- `Maximum execution time`: add timeout or Split In Batches for large datasets
- `Execution limit reached`: activate workflow → settings → increase execution limit

## Credential Storage
Always store secrets in n8n Credential store (Settings → Credentials). Never hardcode in nodes.

## Known Workflow IDs
(Auto-populated by n8n agent after creating/activating workflows)
