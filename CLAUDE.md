# Super-Agent — Claude CLI Context
**Last updated:** 2026-04-29

This file is auto-loaded by `claude -p` on every invocation inside this repo.
It gives Claude CLI situational awareness of the system architecture.

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
| `radiant-appreciation` | Website host — auto-deploys from `website/index.html` |
| `inspiring-cat` (VS Code) | CLI worker container — runs `claude -p`, `gemini`, shell tasks |
| `n8n` | Automation workflows |
| `divine-contentment` | PostgreSQL + pgvector |
| `honest-analysis` | **UNKNOWN** — visible in Railway dashboard, zero codebase references. Not part of the CLI cascade. Needs audit: `railway service list` to confirm purpose. |

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

## COMMON FIX PATTERNS

**Routing misses a request type** → add the missing keyword to the appropriate `_*_KEYWORDS` set in `dispatcher.py`

**Agent has tool access but wrong tools** → check `_OPERATIONAL_KEYWORDS` in `agent_routing.py`

**Website modification task** → github_agent reads `website/index.html`, updates ALL occurrences of the target string, commits, pushes

**n8n task fails** → try 3 paths: Python n8n tools → `run_shell_via_cli_worker` curl → `run_authorized_shell_command` curl

**Claude CLI DOWN** → wait up to 15 min for self-healing watchdog. Check all 4 recovery layers if it doesn't come back.

---

## PENDING ISSUES (as of 2026-04-29)

- **Health:** All systems nominal. DB healthy (4153 stored messages, PostgreSQL). n8n: 56 active, 29 inactive, 0 failures in last hour. 119 interactions loaded for review but Claude Code hit org monthly usage limit — nightly review failed to complete. Gemini CLI still hitting trust-directory block. Disk ~51% used, Cloudinary at 0.18 GB.
- **Priorities for tomorrow:** none
- **Routing observations:** No misroutes observed in 119 interactions reviewed. Keyword routing functioning normally. Gemini CLI trust-directory issue persists — `nightly_review.py` still uses Gemini CLI which needs `GEMINI_CLI_TRUST_WORKSPACE=true` before invocation.
