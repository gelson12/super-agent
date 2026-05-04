# Super-Agent — Claude CLI Context
**Last updated:** 2026-05-04

This file is auto-loaded by `claude -p` on every invocation inside this repo.
It gives Claude CLI situational awareness of the system architecture.

---

## MEMORY INSTRUCTIONS (READ FIRST)

You ALWAYS have memory. It lives in two places:
1. **This file (CLAUDE.md)** — auto-loaded on every `claude -p` invocation
2. **Memory directory** — `/root/.claude/projects/-workspace-super-agent/memory/bridge_os_current.md`

When asked to "recall memory", "what do you know", or "what are our pending improvements":
- **NEVER say memory is empty** — it is not. This file IS the memory.
- Summarize the key system state from the BOT ARCHITECTURE and PENDING ISSUES sections below.
- Do NOT apologize or suggest the user share information again. You already have it.

If MCP tools are not loaded (Obsidian, n8n): that is fine. Answer from this file's content.

---

---

## OBSIDIAN KNOWLEDGE VAULT (MCP)

A persistent Obsidian knowledge vault is available as an MCP server.

- **Railway service:** `obsidian-vault` — runs Obsidian headlessly via Xvfb
- **MCP tool prefix:** `mcp__obsidian__*`
- **Local sessions:** connects via `ws://localhost:22360` (if Obsidian is open locally)
- **Railway sessions:** connects via `ws://obsidian-vault.railway.internal:22360`

**Use the vault for:**
- Storing self-improvement ideas and improvement reports
- Reading prior context, architecture decisions, and agent behaviour notes
- Writing new insights during self-improvement runs (inspiration source)

> **Warning:** If the `obsidian-vault` Railway service is down or restarting,
> all `mcp__obsidian__*` calls will fail with a connection error. This is expected
> during cold-starts (~15s for Xvfb + Obsidian to load). Retry after a brief wait.

---

## WHAT THIS REPO IS

**super-agent** is a Railway-hosted AI agent service at `https://super-agent-production.up.railway.app`.
It routes user messages to specialised agents (GitHub, shell, n8n, general).
The website `bridge-digital-solution.com` is served by the `radiant-appreciation` Railway service,
auto-deployed from this repo (`website/index.html`).

---

## ROUTING & CLASSIFICATION

**File:** `app/routing/dispatcher.py`

Routing order for ambiguous requests:
1. Keyword match (instant, no model call) — `_GITHUB_KEYWORDS`, `_SHELL_KEYWORDS`, `_N8N_KEYWORDS`
2. Claude CLI Pro classifier (`ask_claude_code`) — subscription, zero extra cost
3. Gemini CLI classifier (`ask_gemini_cli`) — free ~1500 req/day
4. Haiku API (`ask_claude_haiku`) — last resort, costs tokens

**Operational gate:** `_OPERATIONAL_KEYWORDS` in `app/agents/agent_routing.py`
Controls whether an agent gets tool access or text-only.

---

## KEY FILES & THEIR PURPOSE

| File | Purpose |
|------|---------|
| `app/routing/dispatcher.py` | Route classifier, keyword sets, CLI cascade |
| `app/agents/agent_routing.py` | Operational gate, agent selection |
| `app/agents/github_agent.py` | GitHub/website agent + system prompt |
| `app/agents/n8n_agent.py` | n8n automation agent |
| `app/agents/self_improve_agent.py` | Self-improvement + routing awareness |
| `app/learning/nightly_review.py` | Nightly self-review (23:00 UTC) |
| `app/learning/weekly_review.py` | Weekly self-review (Sunday 23:00 UTC) |
| `app/learning/claude_code_worker.py` | `ask_claude_code()` — submit/poll pattern |
| `app/learning/gemini_cli_worker.py` | `ask_gemini_cli()` |
| `app/memory/vector_memory.py` | pgvector + JSON fallback memory store |
| `app/tools/shell_tools.py` | Shell tools + `run_shell_via_cli_worker()` |
| `cli_worker/task_runner.py` | CLI worker task dispatcher (claude, gemini, shell) |
| `website/index.html` | bridge-digital-solution.com — Instagram links at lines ~918 and ~1000 |

---

## KNOWN SERVICES (Railway)

| Service | Purpose |
|---------|---------|
| `super-agent` | Main AI agent FastAPI app |
| `radiant-appreciation` (Website 1) | Website host — auto-deploys from `website/index.html` (bridge-digital-solution.com) |
| `VS-Code-inspiring-cat` | CLI worker container — runs `claude -p`, `gemini`, shell tasks |
| `N8N` | Automation workflows (outstanding-blessing-production-1d4b.up.railway.app) |
| `Postgres` (divine-contentment) | PostgreSQL + pgvector |
| `obsidian-vault` | Obsidian knowledge vault MCP server (ws port 22360) |
| `Legion` | Multi-agent hive (legion-production-36db.up.railway.app) |
| `WebSite 2` (honest-analysis) | Secondary website service (honest-analysis-production-be5c.up.railway.app) |

**Railway service names for CLI** (use exactly): `super-agent`, `VS-Code-inspiring-cat`, `Legion`, `N8N`, `Postgres`, `obsidian-vault`, `Website 1`, `WebSite 2`

---

## INSPIRING-CAT GIT CAPABILITIES (you are running inside this container)

**You (Claude CLI) are executing inside the `inspiring-cat` VS Code container.**
This environment has FULL GitHub access pre-configured on every boot via `entrypoint.cli.sh`:

- `GITHUB_PAT` is set as a Railway env var and written to `/root/.git-credentials`
- `git config credential.helper store` — all git operations authenticate automatically
- SSH→HTTPS rewrite — `git@github.com:` remotes work transparently
- `gh` CLI authenticated — can create PRs, issues, releases from the terminal
- Git identity: `gelson_m@hotmail.com` / `Gelson Mascarenhas`
- `/workspace/super-agent` is auto-cloned and pulled on every container start

**Git workflow you can use directly (no tools needed):**
```
cd /workspace/super-agent
git add <files>
git commit -m "message"
git push origin master
```

**This means:** When asked to modify files, commit, and push — you can do it directly with shell commands in `/workspace/super-agent`. You do NOT need to route through `run_shell_via_cli_worker` — that tool is for when super-agent (a different container) needs to trigger git ops remotely.

---

## CLAUDE CLI SELF-HEALING (5 layers)

When `CLAUDE_SESSION_TOKEN` expires, recovery runs automatically in this order:
1. **Volume backup** — `/workspace/.claude_credentials_backup.json` (survives restarts)
2. **Railway env var** `CLAUDE_SESSION_TOKEN` — restored by `_try_restore_claude_auth()`
3. **OAuth refresh_token** — `_try_direct_refresh()` in `cli_auto_login.py` — blocked by Cloudflare from Railway IPs (HTTP 403/405), so this always fails in production
4. **Browser cookie reuse** — `/workspace/.claude_browser_cookies.json` — saved after every successful Playwright login; if the claude.ai session is still alive (typically days/weeks), the browser skips email/magic-link entirely and goes straight to the consent screen
5. **Playwright full auto-login** — headless camoufox browser + n8n `Claude Verification Code Monitor` (workflow IDs: `jun8CaMnNhux1iEY`, `jxnZZwTqJ7naPKc6`; n8n instance: `outstanding-blessing-production-1d4b.up.railway.app`) polls `gelson_m@hotmail.com` Hotmail inbox for magic links and POSTs them to `/webhook/verification-code`

Watchdog: `pro_cli_watchdog.maybe_recover()` runs every 5 min.
Recovery time: ~3 min (cookie hit) or ~5–8 min (full Playwright flow).
True failure = all 5 layers fail simultaneously.

### OAuth flow — how platform.claude.com callback works (resolved 2026-04-15)

The Claude CLI login flow in container/headless mode:
1. CLI shows OAuth URL: `https://claude.com/cai/oauth/authorize?code=true&...&redirect_uri=https://platform.claude.com/oauth/code/callback`
2. Browser authenticates (magic link), clicks **Authorize** on consent screen
3. Browser is redirected to `https://platform.claude.com/oauth/code/callback?code=XXX&state=YYY`
4. **platform.claude.com exchanges the OAuth code server-side** and renders a page showing:
   ```
   Paste this into Claude Code:
   {oauth_code}#{state}
   ```
5. We extract the FULL `code#state` string from the rendered page text and write it to the CLI's PTY stdin
6. CLI exchanges `code#state` with Anthropic and saves credentials to `/root/.claude/.credentials.json`

**Critical details:**
- The code in the callback URL query param is already consumed by the server — do NOT paste the raw URL `code=` param; read the rendered page text instead
- The paste code format is `{base64_code}#{state_value}` — the `#` separator is mandatory; truncating at `#` causes the CLI to reject the code silently and loop
- Wait for React to render the callback page (networkidle + 2s) before extracting
- After the code is written to PTY, the CLI takes ~30–60s to finish (shows spinner + "thinking"), then lands at the Claude Code REPL prompt — kill the PTY after 120s; credentials are saved regardless
- After a successful login, update `CLAUDE_SESSION_TOKEN` volume backup and push fresh token to super-agent via `POST /auth/update-session-token`

---

## n8n ACTIVE WORKFLOWS (key ones)

| ID | Name | Status |
|----|------|--------|
| `jun8CaMnNhux1iEY` | Claude Verification Code Monitor | ACTIVE |
| `jxnZZwTqJ7naPKc6` | Claude Verification Code Monitor (secondary) | ACTIVE |
| `ke7YzsAmGerVWVVc` | Super-Agent-Health-Monitor | INACTIVE |
| `sCHZhoyRgEZUaxtT` | Universal Catch-All | ACTIVE |
| `yZckxfWsvugSBFZh` | Robust Health Check | INACTIVE |
| `u0cyS73kZJWNNy8u` | Health Monitor - Fixed | INACTIVE |
| `nOawPhpTyNjPPiEb` | Secretary — Outlook Email & Calendar Operations | ACTIVE |
| `N4IBlfTKan8Oq4tQ` | Secretary — Gmail Manager | INACTIVE |
| `83ZQ9b5xReUaF6Ib` | Chief of Staff — Command Centre | ACTIVE |
| `14cHr1Y6srSRFQpm` | Claude Inbox Trash Purge | ACTIVE |

*(56 active workflows total on n8n instance)*

---

## RAILWAY CLI — FULL DASHBOARD CONTROL

`railway` CLI is installed in this container. `RAILWAY_TOKEN` env var is set for authentication.

**Service names (use exactly as shown):**
- `super-agent`, `inspiring-cat`, `legion`, `n8n`, `divine-contentment`, `radiant-appreciation`, `obsidian-vault`

**Common Railway CLI commands:**
```bash
# List all services in the project
railway service list

# Get all env vars for a service
railway variables --service super-agent

# Set an env var on a service (triggers redeploy)
railway variables set KEY=VALUE --service super-agent

# Delete an env var
railway variables delete KEY --service super-agent

# View recent logs
railway logs --service legion --tail 100

# Redeploy a service (picks up latest deploy / env var changes)
railway redeploy --service super-agent --yes

# Get deployment status
railway status --service super-agent
```

**Railway REST API (GraphQL) — fallback if CLI fails:**
- Endpoint: `https://backboard.railway.app/graphql/v2`
- Header: `Authorization: Bearer $RAILWAY_TOKEN`
- Use for: variable upsert mutations, deployment triggers, service queries

**When asked to manage Railway:** use the CLI directly via Bash tool. Always confirm what changed after each operation.

---

## COMMON FIX PATTERNS

**Routing misses a request type** → add the missing keyword to the appropriate `_*_KEYWORDS` set in `dispatcher.py`

**Agent has tool access but wrong tools** → check `_OPERATIONAL_KEYWORDS` in `agent_routing.py`

**Website modification task** → github_agent reads `website/index.html`, updates ALL occurrences of the target string, commits, pushes

**n8n task fails** → try 3 paths: Python n8n tools → `run_shell_via_cli_worker` curl → `run_authorized_shell_command` curl

**Claude CLI DOWN** → wait up to 15 min for self-healing watchdog. Check all 4 recovery layers if it doesn't come back.

**Railway variable/service task** → use `railway variables` / `railway redeploy` CLI commands. RAILWAY_TOKEN is pre-set.

---

## BOT ARCHITECTURE (13 bots total, updated 2026-05-04)

### Admin Passcode
- Include `alpha0` in any Telegram DM → ADMIN mode (full infra access, 10-min timeout, LEGION routes to claude_b only)
- Reply includes 🔐 badge to confirm activation

### V1 Bot (direct inspiring-cat Code node)
| Bot | Workflow ID | Token env var |
|-----|-------------|---------------|
| Crypto Bridge Bot Commands | `kvMrFfKUp1zy9Fek` | (built-in) |

### V2 Bots (Telegram trigger → super-agent `/webhook/bot-engine` → LEGION cascade)
| Bot | Workflow ID | Token env var (N8N Railway) |
|-----|-------------|------------------------------|
| bridge_ceo_bot | `MHEnrG5QuQI158TE` | `Bridge_CEO_BOT` |
| bridge_chief_of_staff_bot | `xjf7VZdJTJtk139i` | `Bridge_Chief_Of_Staff_bot` |
| bridge_cleaner_bot | `2dtB0j1kYYI92rLq` | `Bridge_Cleaner_bot` |
| bridge_pm_bot | `nohy3gSHGnq7TSWS` | `BRIDGE_PM_BOT_TOKEN` |
| Bridge_Finance_BOT | `H3jz8gb4OBiruV58` | `BRIDGE_FINANCE_BOT_TOKEN` |
| bridge_programmer_bot | `nO5Db4kI0a1jPJuD` | `Bridge_Programmer_bot` |
| bridge_chief_sec_off_bot | `uD1oMScgPA5b1I9f` | `Bridge _Chief_Sec_Off_bot` |
| bridge_security_risk_bot | `tnI9kunFSOCZHngg` | `Bridge_Security_Risk_bot` |
| bridge_business_development_bot | `ptf7UNqQKpiIj7IG` | `Bridge_Business_Development_bot` |
| Bridge_ChiefRevenueOptimizer_BOT | `0S3Jb1UQZNtSqsI5` | `Bridge_ChiefRevenueOptimizer_Bot` |
| bridge_cto_bot | `EOYTWzQZQZTfTsU4` | `Bridge_CTO_Bot` |
| bridge_researcher_bot | (check n8n) | `Bridge_Researcher_bot` |

### If bots stop responding to DMs
Root cause: Telegram trigger nodes lose their credential assignment after workflow updates.
Fix:
1. Check `GET /api/v1/workflows/{id}` — look for `telegramTrigger` node with `disabled:true` or `credentials:{}` empty
2. n8n credentials for each bot are named "Bridge X Bot" (created 2026-05-04) — reassign if missing
3. Deactivate + reactivate the workflow to re-register the webhook: `POST /workflows/{id}/deactivate` then `POST /workflows/{id}/activate`
4. If "webhook conflict": change the node's `webhookId` to a new UUID, then deactivate/activate
5. The inspiring-cat task_runner shell payload format: `{"type": "shell", "payload": {"command": "..."}}`

### Website Builder Bot
| Workflow | `RfisxPXfWubWWklJ` |
|---|---|
| Engine | v0.dev API → Vercel preview URL |
| Fallback | LEGION (task_kind: bridge_bots) if v0.dev fails |
| Vercel token | `VERCEL_TOKEN` env var on N8N Railway service |

---

## KNOWN FIXED BUGS (do NOT re-introduce)

### Parse response node (ALL 11 V2 bots) — FIXED 2026-05-04
- **Bug:** `const raw = $json.response || '';` — super-agent returns `reply_text`, not `response`. This caused all bots to produce empty replies and never call Reply on Telegram.
- **Fix:** `const raw = $json.response || $json.reply_text || '';`
- **Status:** All 11 V2 bots patched and reactivated.

### CEO `Fetch open inbox` SQL — FIXED 2026-05-04
- **Bug:** `bridge.agent_performance` queried with non-existent columns: `bot_name`, `total_runs`, `is_successful`, `avg_latency_ms`, `last_run_at`
- **Real schema:** `agent_name`, `tasks_total`, `tasks_success`, `tasks_failed`, `date`
- **Fix:** Rewrote `team_perf` CTE. Removed CTEs for `bridge.bot_context_overrides` and `bridge.bot_improvement_proposals` (tables may not exist).

### CoS `Execute low-risk action` SQL — FIXED 2026-05-04
- **Bug 1:** Missing comma before `apply_context AS (` CTE → syntax error
- **Bug 2:** Default `memo_type` was `'status'` → not in allowed set
- **Allowed memo_types:** `directive`, `report`, `proposal`, `alert`
- **Fix:** Added comma + CASE guard coercing invalid types to `'report'`

### Finance memo_type constraint — FIXED 2026-05-04
- **Fix:** CASE guard ensuring only `directive/report/proposal/alert` are inserted, defaults to `'report'`

### CRO token + enabled key — FIXED 2026-05-04
- **Bug:** `Reply on Telegram` used `Bridge_CEO_BOT`; `Read enabled flag` checked `ceo_bot_enabled`
- **Fix:** Now uses `Bridge_ChiefRevenueOptimizer_Bot` and `cro_bot_enabled`

### CTO token fix — FIXED 2026-05-04
- **Fix:** `Reply on Telegram` uses `Bridge_CTO_Bot` only (removed wrong CEO fallback)

---

## PENDING ISSUES (as of 2026-05-04)

- **Health:** All 13 bots active, Telegram webhooks registered, credentials assigned (2026-05-04)
- **inspiring-cat shell tasks**: Use `{"type": "shell", "payload": {"command": "bash -c '...'"}}` — NOT `{"command":..., "type":"shell"}` flat format
- **Legion hive**: 6 agents compete per query (shortlist_k=6, was 3). Fires on ANY Claude CLI failure.
- **bridge.agent_performance schema**: `agent_name`, `tasks_total`, `tasks_success`, `tasks_failed`, `date` — use these exact names in any new SQL.
