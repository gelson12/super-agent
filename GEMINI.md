# Super-Agent — Gemini CLI Context
**Last updated:** 2026-05-16

This file is auto-loaded by `gemini --prompt` on every invocation inside this repo.
It gives Gemini CLI situational awareness of the system architecture.

---

## WHAT THIS REPO IS

**super-agent** is a Railway-hosted AI agent service at `https://super-agent-production.up.railway.app`.
Routes user messages to specialised agents: GitHub, shell, n8n, general.
Website `bridge-digital-solution.com` is served by the `radiant-appreciation` Railway service,
auto-deployed from this repo (`website/index.html`).

---

## ROUTING & CLASSIFICATION

**File:** `app/routing/dispatcher.py`

When used as a classifier, respond ONLY in this format:
```
CATEGORY: <GITHUB|SHELL|N8N|GENERAL>
CONFIDENCE: <0.0–1.0>
```

Routing order (you are Tier 2 — Gemini CLI):
1. Keyword match (instant) — `_GITHUB_KEYWORDS`, `_SHELL_KEYWORDS`, `_N8N_KEYWORDS`
2. Claude CLI Pro classifier — first attempt
3. **Gemini CLI classifier (you)** — fallback when Claude CLI Pro fails/unavailable
4. Haiku API — last resort, costs tokens

Classification categories:
- **GITHUB** — modifying repos, website, files, committing, pushing, Instagram links, HTML changes
- **SHELL** — terminal commands, Flutter builds, APK builds, cloning, git operations
- **N8N** — workflows, automations, webhooks, n8n triggers, monitoring
- **GENERAL** — everything else (conversational, explanatory, analysis)

---

## KEY FILES

| File | Purpose |
|------|---------|
| `app/routing/dispatcher.py` | Route classifier + keyword sets |
| `app/agents/agent_routing.py` | Operational gate, agent selection |
| `app/agents/github_agent.py` | GitHub/website modifications |
| `app/agents/n8n_agent.py` | n8n automation agent |
| `app/learning/nightly_review.py` | Nightly self-review (23:00 UTC) |
| `app/learning/claude_code_worker.py` | `ask_claude_code()` — submit/poll pattern |
| `app/learning/gemini_cli_worker.py` | `ask_gemini_cli()` |
| `app/memory/vector_memory.py` | pgvector + JSON fallback memory |
| `app/tools/shell_tools.py` | Shell tools + `run_shell_via_cli_worker()` |
| `cli_worker/task_runner.py` | Task dispatcher (claude_pro, gemini_cli, shell) |
| `website/index.html` | bridge-digital-solution.com — Instagram links at lines ~918 and ~1000 |

---

## RAILWAY SERVICES

| Service | URL / Purpose |
|---------|--------------|
| `super-agent` | Main FastAPI AI agent |
| `radiant-appreciation` | Website — auto-deploys from `website/index.html` |
| `inspiring-cat` | CLI worker container — runs `claude -p`, `gemini`, shell tasks |
| `n8n` | Automation workflows |
| `divine-contentment` | PostgreSQL + pgvector |
| `honest-analysis` | **UNKNOWN** — needs audit |

---

## N8N ACTIVE WORKFLOWS (key ones)

| ID | Name | Status |
|----|------|--------|
| `jun8CaMnNhux1iEY` | Claude Verification Code Monitor | ACTIVE |
| `jxnZZwTqJ7naPKc6` | Claude Verification Code Monitor (secondary) | ACTIVE |
| `ke7YzsAmGerVWVVc` | Super-Agent-Health-Monitor | INACTIVE |
| `sCHZhoyRgEZUaxtT` | Universal Catch-All | ACTIVE |
| `nOawPhpTyNjPPiEb` | Secretary — Outlook Email & Calendar Operations | ACTIVE |
| `N4IBlfTKan8Oq4tQ` | Secretary — Gmail Manager | INACTIVE |
| `83ZQ9b5xReUaF6Ib` | Chief of Staff — Command Centre | ACTIVE |
| `14cHr1Y6srSRFQpm` | Claude Inbox Trash Purge | ACTIVE |

*(71 active workflows total on n8n instance)*

---

## INSPIRING-CAT GIT CAPABILITIES (you are running inside this container)

**You (Gemini CLI) are executing inside the `inspiring-cat` VS Code container.**
This environment has FULL GitHub access pre-configured on every boot:

- `GITHUB_PAT` written to `/root/.git-credentials` — all git operations authenticate automatically
- `gh` CLI authenticated — can create PRs, issues from terminal
- Git identity: `gelson_m@hotmail.com` / `Gelson Mascarenhas`
- `/workspace/super-agent` is auto-cloned and pulled on every container start

**Direct git workflow (no extra tools needed):**
```
cd /workspace/super-agent && git add <files> && git commit -m "msg" && git push origin master
```

---

## COMMON PATTERNS

- **Routing miss** → add keyword to appropriate `_*_KEYWORDS` set in `dispatcher.py`
- **Website change** → github_agent reads `website/index.html`, updates ALL occurrences, commits + pushes
- **n8n task** → try Python tools first, then `run_shell_via_cli_worker` curl, then `run_authorized_shell_command` curl
- **Claude CLI DOWN** → wait up to 15 min for 4-layer self-healing watchdog

---


## PENDING ISSUES (2026-05-16)

- **Health:** Error rate steady at ~1.1% across ~3,078 interactions (agent_metrics). Insight log: 48 entries with 0 errors (narrow scope). n8n: 71 active workflows (105 total), reachable ✅ — occasional transient failures (Gemini 429 auto-recovered, bridge bot ECONNRESETs from stale memo bloat). Disk ~47.3% used (~1,096 GB free). Nightly review 2026-05-16 failed — Claude Code CLI timed out after 130s (11th consecutive nightly failure: 05-06 through 05-16). Cost ledger: 26 entries, all model = UNKNOWN. Model attribution remains blind in both cost_ledger and prod_usage_log.
- **Priorities for tomorrow:** none
- **Routing observations:** No misroutes detected across GITHUB, SHELL, or CLAUDE routes. Insight log shows 3 GitHub-routed queries (outlook_qa sessions) routed successfully by github_keywords. All bridge bot sessions routed by 'forced' via webhook-bot-engine. Haiku + Gemini parallel classifier nominal. Nightly review 05-16 attempted but Claude Code CLI timed out at 130s — 11 consecutive nightly failures — strongly recommend switching nightly review to Gemini CLI as default. 2 bridge bots remain deactivated (bridge_ceo_bot SQL column mismatch, bridge_chief_sec_off_bot webhook config). Gemini free-tier key `AIzaSyA6qcqiigyQOkdRcugrEoJKABU6wAYeq9c` zero quota across all models — Chief of Staff health monitor breaks every 20min.
