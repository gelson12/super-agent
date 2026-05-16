# Super-Agent — Claude CLI Context
**Last updated:** 2026-05-16

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

## ROUTING & CLASSIFICATION — UPDATED ARCHITECTURE

**File:** `app/routing/dispatcher.py`

Routing order for ambiguous requests:
1. Keyword match (instant, no model call) — `_GITHUB_KEYWORDS`, `_SHELL_KEYWORDS`, `_N8N_KEYWORDS`
2. **Parallel classifier** — Haiku + Gemini CLI fire simultaneously; first valid response wins
   (Gemini quota failure no longer kills routing — Haiku is now primary)
3. Keyword fallback (if both classifiers fail)

**Agent execution — TIER 0 PARALLEL (new):**
- Claude CLI + Legion (Groq/Cerebras/GH Models) + Gemini CLI all fire simultaneously
- First quality response wins, rest cancelled immediately
- Target latency: 1–3 seconds for most queries
- Legion is now Tier 0 (first-line racer), not last-resort fallback

**Tier 1 (only if Tier 0 fully fails):** DeepSeek LangGraph (cheap, full tools)
**Tier 2 (absolute last resort):** Anthropic API LangGraph (Sonnet)

**Operational gate:** `_OPERATIONAL_KEYWORDS` in `app/agents/agent_routing.py`
Controls whether an agent gets tool access or text-only.

---

## KEY FILES & THEIR PURPOSE

| File | Purpose |
|------|---------|
| `app/routing/dispatcher.py` | Route classifier, keyword sets, CLI cascade |
| `app/routing/classifier.py` | Parallel Haiku+Gemini classifier (Gemini no longer sole classifier) |
| `app/agents/agent_routing.py` | Parallel Tier 0 routing, operational gate |
| `app/agents/agent_planner.py` | Parallel plan competition (truly concurrent now) |
| `app/agents/github_agent.py` | GitHub/website agent + system prompt |
| `app/agents/n8n_agent.py` | n8n automation agent |
| `app/agents/self_improve_agent.py` | Self-improvement + routing awareness |
| `app/learning/nightly_review.py` | Nightly self-review (23:00 UTC) |
| `app/learning/weekly_review.py` | Weekly self-review (Sunday 23:00 UTC) |
| `app/learning/claude_code_worker.py` | `ask_claude_code()` — submit/poll pattern |
| `app/learning/gemini_cli_worker.py` | `ask_gemini_cli()` |
| `app/memory/session.py` | PostgreSQL session memory, compressed context |
| `app/memory/vector_memory.py` | pgvector + JSON fallback memory store |
| `app/tools/shell_tools.py` | Shell tools + `run_shell_via_cli_worker()` |
| `cli_worker/task_runner.py` | CLI worker — now injects history into claude_pro tasks |
| `website/index.html` | bridge-digital-solution.com — Instagram links at lines ~918 and ~1000 |

---

## KNOWN SERVICES (Railway)

| Service | Purpose |
|---------|---------|
| `super-agent` | Main AI agent FastAPI app |
| `radiant-appreciation` (Website 1) | Website host — auto-deploys from `website/index.html` (bridge-digital-solution.com) |
| `VS-Code-inspiring-cat` | CLI worker container — runs `claude -p`, `gemini --skip-trust`, shell tasks |
| `N8N` | Automation workflows (outstanding-blessing-production-1d4b.up.railway.app) |
| `Postgres` (divine-contentment) | PostgreSQL + pgvector |
| `obsidian-vault` | Obsidian knowledge vault MCP server (ws port 22360) |
| `Legion` | Multi-agent hive — Groq, Cerebras, GH Models, OpenRouter, HF, Ollama |
| `WebSite 2` (honest-analysis) | Secondary website service |

**Railway service names for CLI** (use exactly): `super-agent`, `VS-Code-inspiring-cat`, `Legion`, `N8N`, `Postgres`, `obsidian-vault`, `Website 1`, `WebSite 2`

---

## ⚠️ RAILWAY API BLOCKED FROM INSIDE CONTAINERS

**Cloudflare CF 1010 blocks `backboard.railway.app` from Railway container IPs.**

These tools WILL return 403 when called from inside any Railway container:
- `railway_list_variables`
- `railway_list_services`
- `railway_set_variable`
- `railway_get_logs` (may also fail)
- `railway_get_deployment_status` (may also fail)

**Workarounds:**
- To READ env vars: use `run_shell_command("printenv")` — already injected at startup
- To UPDATE an env var: use `POST /webhook/github-scheduled-sync` (triggers GitHub Actions relay)
  OR fire `repository_dispatch` via GITHUB_PAT to repo `gelson12/super-agent`
- To check recent errors: use `/activity/recent?limit=100` instead of `railway_get_logs`
- To check service health: use `GET /admin/infrastructure-info` (internal endpoint, always works)

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
```bash
cd /workspace/super-agent
git add <files>
git commit -m "message"
git push origin master
```

---

## CLAUDE CLI SELF-HEALING (5 layers)

When `CLAUDE_SESSION_TOKEN` expires, recovery runs automatically in this order:
1. **Volume backup** — `/workspace/.claude_credentials_backup.json` (survives restarts)
2. **Railway env var** `CLAUDE_SESSION_TOKEN` — restored by `_try_restore_claude_auth()`
3. **OAuth refresh_token** — blocked by Cloudflare from Railway IPs (HTTP 403/405); always fails in production
4. **Browser cookie reuse** — `/workspace/.claude_browser_cookies.json` — if claude.ai session still alive
5. **Playwright full auto-login** — headless camoufox + n8n `Claude Verification Code Monitor`
   (workflow IDs: `jun8CaMnNhux1iEY`, `jxnZZwTqJ7naPKc6`)
   polls `gelson_m@hotmail.com` for magic links, POSTs to `/webhook/verification-code`

Watchdog: `pro_cli_watchdog.maybe_recover()` runs every 5 min.
Recovery time: ~3 min (cookie hit) or ~5–8 min (full Playwright flow).

### GEMINI CLI — trust directory fix
`GEMINI_CLI_TRUST_WORKSPACE=true` is now set in `_CLI_ENV` in `task_runner.py`.
All gemini calls also pass `--skip-trust` flag directly. No more trust directory errors.

---

## n8n ACTIVE WORKFLOWS (key ones)

| ID | Name | Status | Action needed |
|----|------|--------|---------------|
| `jun8CaMnNhux1iEY` | Claude Verification Code Monitor | ACTIVE | ✅ |
| `jxnZZwTqJ7naPKc6` | Claude Verification Code Monitor (secondary) | ACTIVE | ✅ |
| `ke7YzsAmGerVWVVc` | Super-Agent-Health-Monitor | **INACTIVE** | ⚠️ Activate + add Telegram node |
| `sCHZhoyRgEZUaxtT` | Universal Catch-All | ACTIVE | ✅ |
| `yZckxfWsvugSBFZh` | Robust Health Check | **INACTIVE** | ⚠️ Activate + add Telegram node |
| `u0cyS73kZJWNNy8u` | Health Monitor - Fixed | **INACTIVE** | ⚠️ Activate + add Telegram node |
| `nOawPhpTyNjPPiEb` | Secretary — Outlook Email & Calendar Operations | ACTIVE | ✅ |
| `N4IBlfTKan8Oq4tQ` | Secretary — Gmail Manager | **INACTIVE** | ⚠️ Activate |
| `83ZQ9b5xReUaF6Ib` | Chief of Staff — Command Centre | ACTIVE | ✅ |
| `14cHr1Y6srSRFQpm` | Claude Inbox Trash Purge | ACTIVE | ✅ |

*(71 active workflows total on n8n instance)*

---

## RAILWAY CLI — FULL DASHBOARD CONTROL

`railway` CLI is installed in this container. `RAILWAY_TOKEN` env var is set for authentication.

**Service names (use exactly as shown):**
- `super-agent`, `inspiring-cat`, `legion`, `n8n`, `divine-contentment`, `radiant-appreciation`, `obsidian-vault`

**Common Railway CLI commands:**
```bash
railway service list
railway variables --service super-agent
railway variables set KEY=VALUE --service super-agent
railway variables delete KEY --service super-agent
railway logs --service legion --tail 100
railway redeploy --service super-agent --yes
railway status --service super-agent
```

---

## COMMON FIX PATTERNS

**Routing misses a request type** → add the missing keyword to the appropriate `_*_KEYWORDS` set in `dispatcher.py`

**Agent has tool access but wrong tools** → check `_OPERATIONAL_KEYWORDS` in `agent_routing.py`

**Website modification task** → github_agent reads `website/index.html`, updates ALL occurrences of the target string, commits, pushes

**n8n task fails** → try 3 paths: Python n8n tools → `run_shell_via_cli_worker` curl → `run_authorized_shell_command` curl

**Claude CLI DOWN** → wait up to 15 min for self-healing watchdog. Check all 5 recovery layers if it doesn't come back.

**Railway variable/service task** → use `railway variables` / `railway redeploy` CLI commands. RAILWAY_TOKEN is pre-set.

**Telegram bots silent** →
1. Check if health monitor workflows are ACTIVE (ke7YzsAmGerVWVVc, yZckxfWsvugSBFZh, u0cyS73kZJWNNy8u)
2. Check if workflow has Telegram notification node
3. Alerting routes through Gmail via n8n by default — Telegram nodes must be added manually
4. Check `GET /api/v1/workflows/{id}` — look for `telegramTrigger` node with `disabled:true` or `credentials:{}` empty

---

## BOT ARCHITECTURE (13 bots total, updated 2026-05-05)

### Admin Passcode
- Include `alpha0` in any Telegram DM → ADMIN mode (full infra access, 10-min timeout)
- Reply includes 🔐 badge to confirm activation
- **Fixed 2026-05-05:** Logger callable bug in dispatcher.py lines 1086/1096 — `_log()` → `_log.info()`

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
| bridge_chief_sec_off_bot | `uD1oMScgPA5b1I9f` | `Bridge_Chief_Sec_Off_bot` ⚠️ (remove space from env var name) |
| bridge_security_risk_bot | `tnI9kunFSOCZHngg` | `Bridge_Security_Risk_bot` |
| bridge_business_development_bot | `ptf7UNqQKpiIj7IG` | `Bridge_Business_Development_bot` |
| Bridge_ChiefRevenueOptimizer_BOT | `0S3Jb1UQZNtSqsI5` | `Bridge_ChiefRevenueOptimizer_Bot` |
| bridge_cto_bot | `EOYTWzQZQZTfTsU4` | `Bridge_CTO_Bot` |
| bridge_researcher_bot | `zzCNcD2z69dedoF6` | `Bridge_Researcher_bot` |

### ⚠️ ENV VAR BUG — Fix immediately
`Bridge _Chief_Sec_Off_bot` has a **space** in the name. Railway will never resolve it.
Rename to: `Bridge_Chief_Sec_Off_bot` (no space after Bridge_)

### If bots stop responding to DMs
Root cause: Telegram trigger nodes lose their credential assignment after workflow updates.
Fix:
1. Check `GET /api/v1/workflows/{id}` — look for `telegramTrigger` with `disabled:true` or `credentials:{}` empty
2. n8n credentials for each bot are named "Bridge X Bot" — reassign if missing
3. Deactivate + reactivate to re-register webhook
4. If "webhook conflict": change the node's `webhookId` to a new UUID, then deactivate/activate

### Website Builder Bot
| Workflow | `RfisxPXfWubWWklJ` |
|---|---|
| Engine | v0.dev API → Vercel preview URL |
| Fallback | LEGION (task_kind: bridge_bots) if v0.dev fails |

---

## KNOWN FIXED BUGS (do NOT re-introduce)

### Logger callable bug — FIXED 2026-05-05
- **Bug:** `_log(...)` called as function at dispatcher.py lines 1023, 1086, 1096
- **Error:** `'Logger' object is not callable` — crashed every alpha0 authorization
- **Fix:** Changed to `_log.info(...)` at all 3 locations

### Gemini CLI trust directory — FIXED 2026-05-05
- **Bug:** Gemini CLI not running in trusted directory (headless/automated environment)
- **Fix:** Added `--skip-trust` flag + `GEMINI_CLI_TRUST_WORKSPACE=true` in `_CLI_ENV`

### Claude CLI context amnesia — FIXED 2026-05-05
- **Bug:** `task_runner.py` called `claude -p "{prompt}"` with no conversation history
- **Fix:** `_build_claude_prompt_with_history()` injects compressed session history before every CLI call

### Serial fallback chain — FIXED 2026-05-05
- **Bug:** Fallback chain was serial — waited for each model to time out (120s each)
- **Fix:** Tier 0 now fires Claude CLI + Legion + Gemini simultaneously; first quality response wins

### Legion as last resort — FIXED 2026-05-05
- **Bug:** Legion (Groq/Cerebras/GH Models) was Tier 5 — only reached after 4 timeouts
- **Fix:** Legion promoted to Tier 0 — now a first-line parallel racer alongside CLI

### Gemini sole classifier — FIXED 2026-05-05
- **Bug:** Gemini Flash was the only classifier — quota exhaustion killed all routing
- **Fix:** Haiku is now primary classifier; Gemini runs in parallel as second opinion

### agent_planner serial competition — FIXED 2026-05-05
- **Bug:** `compete_and_plan()` called `.result()` serially — plans A/B/C ran sequentially
- **Fix:** Uses `as_completed()` so all plans run in true parallel

### Parse response node (ALL 11 V2 bots) — FIXED 2026-05-04
- **Bug:** `$json.response` → super-agent returns `reply_text`, not `response` → empty bot replies
- **Fix:** `$json.response || $json.reply_text || ''`

### CEO `Fetch open inbox` SQL — FIXED 2026-05-04
- **Fix:** Rewrote `team_perf` CTE using real schema: `agent_name`, `tasks_total`, `tasks_success`, `tasks_failed`, `date`

### CoS `Execute low-risk action` SQL — FIXED 2026-05-04
- **Fix:** Added missing comma + CASE guard for `memo_type`

### Finance memo_type constraint — FIXED 2026-05-04
- **Fix:** CASE guard ensuring only `directive/report/proposal/alert` are inserted

### CRO token + enabled key — FIXED 2026-05-04
- **Fix:** Uses `Bridge_ChiefRevenueOptimizer_Bot` and `cro_bot_enabled`

### CTO token fix — FIXED 2026-05-04
- **Fix:** `Reply on Telegram` uses `Bridge_CTO_Bot` only

---

## PENDING ISSUES (2026-05-16)

- **Health:** Error rate steady at ~1.1% across ~3,078 interactions (agent_metrics). Insight log: 48 entries with 0 errors (narrow scope). n8n: 71 active workflows (105 total), reachable ✅ — occasional transient failures (Gemini 429 auto-recovered, bridge bot ECONNRESETs from stale memo bloat). Disk ~47.3% used (~1,096 GB free). Nightly review 2026-05-16 failed — Claude Code CLI timed out after 130s (11th consecutive nightly failure: 05-06 through 05-16). Cost ledger: 26 entries, all model = UNKNOWN. Model attribution remains blind in both cost_ledger and prod_usage_log.
- **Priorities for tomorrow:** none
- **Routing observations:** No misroutes detected across GITHUB, SHELL, or CLAUDE routes. Insight log shows 3 GitHub-routed queries (outlook_qa sessions) routed successfully by github_keywords. All bridge bot sessions routed by 'forced' via webhook-bot-engine. Haiku + Gemini parallel classifier nominal. Nightly review 05-16 attempted but Claude Code CLI timed out at 130s — 11 consecutive nightly failures still unresolved. 2 bridge bots remain deactivated (bridge_ceo_bot SQL column mismatch, bridge_chief_sec_off_bot webhook config). Gemini free-tier key `AIzaSyA6qcqiigyQOkdRcugrEoJKABU6wAYeq9c` zero quota across all models — Chief of Staff health monitor breaks every 20min.
