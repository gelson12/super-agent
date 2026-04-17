---
type: reference
tags: [errors, fixes, troubleshooting, railway]
date: 2026-04-17
---

# Error → Fix Database

## Railway / Infrastructure

### "Application not found" / "Instance unavailable"
**Fix:** Service crashed or stopped. `railway_redeploy(service)` → wait 60s → verify.
**Root cause:** OOM kill, crash loop, failed deploy, missing env var on startup.

### "Connection refused" port 8001 (super-agent API)
**Fix:** `supervisorctl restart uvicorn` in container shell.
**Root cause:** Python import error at startup (usually missing env var or package).

### "Connection refused" port 3001 (VS Code / code-server)
**Fix:** `supervisorctl restart code-server`

### "502 Bad Gateway" on Railway public URL
**Fix:** Service is down. Check `railway_get_logs` → identify crash reason → fix → redeploy.
**Common cause:** OOM (add memory), import error (check requirements.txt).

---

## n8n

### "Cannot connect to n8n" / 502 on n8n URL
**Fix:** n8n Railway service needs redeploy. Check logs first for crash reason.
**Steps:** `railway_get_logs(service="n8n")` → diagnose → `railway_redeploy("n8n")`

### "401 Unauthorized" on n8n API calls
**Fix:** N8N_API_KEY env var is wrong or missing. Check with `railway_list_variables`.

### n8n execution stuck / hung
**Fix:** `n8n_list_executions` → look for status=running → `n8n_deactivate_workflow` then re-activate.

### "Webhook URL not reachable"
**Fix:** Confirm N8N_WEBHOOK_URL env var matches the Railway public URL of the n8n service.

---

## GitHub

### "404 Not Found" reading a file
**Fix:** Try alternate branches: `master` → `main` → `develop`. Or use `github_list_files` to find path.

### "401 Bad credentials" / "Bad PAT"
**Fix:** GITHUB_PAT expired or missing. `railway_list_variables` to confirm it's set.
**Note:** Railway secrets are never shown — if it's set, regenerate and update in Railway.

### "409 Conflict" on file update
**Fix:** SHA mismatch — re-read the file first to get current SHA, then update.

---

## Database / PostgreSQL

### "SSL connection required"
**Fix:** DATABASE_URL must end with `?sslmode=require` (Railway PostgreSQL requires SSL).

### "too many connections"
**Fix:** Connection pool exhausted. Check for leaked connections. Restart service to clear pool.

### "relation does not exist"
**Fix:** Table missing — run migration. Check `db_run_safe_query` against schema.

---

## Python / FastAPI

### "ModuleNotFoundError" on startup
**Fix:** Package missing from `requirements.txt`. Add it, push to git → Railway redeploys.

### "422 Unprocessable Entity" on API call
**Fix:** Request body doesn't match Pydantic schema. Check field names, types, required vs optional.

### "RecursionError" / "maximum recursion depth"
**Fix:** Circular import or infinite loop. Check recent changes to imports.

---

## Claude / AI APIs

### "[Claude error: rate limit exceeded]"
**Fix:** Transient. Retry after 30s with exponential backoff. If persistent, check Anthropic quota.

### "[DeepSeek error: insufficient balance]"
**Fix:** Top up at platform.deepseek.com — credits are depleted.

### "[Claude error: APIConnectionError]"
**Fix:** Transient network issue. Retry. If persistent, check Railway outbound network.

---

## Obsidian Vault MCP

### Vault tools returning "connection refused" / empty results
**Fix:** Vault MCP server (port 22360) may be down. Check Railway obsidian-vault service.
**Steps:** `railway_get_logs(service="obsidian-vault")` → redeploy if crashed.
