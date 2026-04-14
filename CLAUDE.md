# Super-Agent — Claude CLI Context
**Last updated:** 2026-04-14

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

**This means:** When asked to modify files, commit, or push — you can do it directly with shell commands in `/workspace/super-agent`. You do NOT need to route through `run_shell_via_cli_worker` — that tool is for when super-agent (a different container) needs to trigger git ops remotely.

---

## CLAUDE CLI SELF-HEALING (4 layers)

When `CLAUDE_SESSION_TOKEN` expires, recovery runs automatically:
1. Volume backup — `/workspace/.claude_credentials_backup.json`
2. Railway env var `CLAUDE_SESSION_TOKEN` — restored by `_try_restore_claude_auth()`
3. OAuth `refresh_token` — `_try_direct_refresh()` in `cli_auto_login.py`
4. Playwright auto-login — headless browser + n8n `Claude-Verification-Monitor` (ID: `jxnZZwTqJ7naPKc6`) reads code from `gelson_m@hotmail.com`

Watchdog: `pro_cli_watchdog.maybe_recover()` runs every 5 min.
Recovery time: up to ~15 min. True failure = all 4 layers fail simultaneously.

---

## n8n ACTIVE WORKFLOWS

| ID | Name | Status |
|----|------|--------|
| `jxnZZwTqJ7naPKc6` | Claude-Verification-Monitor | ACTIVE (Outlook OAuth) |
| `ke7YzsAmGerVWVVc` | Super-Agent-Health-Monitor | ACTIVE |
| `sCHZhoyRgEZUaxtT` | Universal Catch-All | ACTIVE |
| `yZckxfWsvugSBFZh` | Robust Health Check | ACTIVE |
| `u0cyS73kZJWNNy8u` | Health Monitor - Fixed | ACTIVE |

---

## COMMON FIX PATTERNS

**Routing misses a request type** → add the missing keyword to the appropriate `_*_KEYWORDS` set in `dispatcher.py`

**Agent has tool access but wrong tools** → check `_OPERATIONAL_KEYWORDS` in `agent_routing.py`

**Website modification task** → github_agent reads `website/index.html`, updates ALL occurrences of the target string, commits, pushes

**n8n task fails** → try 3 paths: Python n8n tools → `run_shell_via_cli_worker` curl → `run_authorized_shell_command` curl

**Claude CLI DOWN** → wait up to 15 min for self-healing watchdog. Check all 4 recovery layers if it doesn't come back.

---

## PENDING ISSUES (as of 2026-04-13)

- Anthropic API account has NO CREDITS — Haiku/Sonnet/Opus API calls will fail. Top up at console.anthropic.com.
- Verify inspiring-cat shell task type works end-to-end after redeploy: `run_shell_via_cli_worker("git -C /workspace/super-agent pull")`
