# Super-Agent — Gemini CLI Context
**Last updated:** 2026-05-14

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

*(70 active workflows total on n8n instance)*

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


## PENDING ISSUES (2026-05-14)

- **Health:** Error rate stable at 1.2% across ~2,946 interactions (up from ~2,865). n8n: 68 active workflows, reachable ✅ — occasional transient failures (2 at peak, 0 at latest check), all auto-recovered. Disk ~48.6% used (~1,068 GB free). Budget usage stable. Nightly review 2026-05-14 failed — Claude Code CLI timed out after 130s (9th consecutive nightly failure: 05-06 through 05-14, all due to org quota exhaustion / CLI timeout). Model column in cost_ledger: now 47 entries total, all still show "UNKNOWN" — model attribution remains unfixed. Improvement cycle identified tool caching for bridge agent system prompts as next optimization target.
- **Priorities for tomorrow:** none
- **Routing observations:** No misroutes detected. Haiku + Gemini parallel classifier working as expected. Nightly review 05-14 reviewed 77 interactions before timing out. Claude Code CLI nightly review now at 9 consecutive failures — still relying on Claude Code despite persistent org quota issues. Strongly recommend switching nightly review to Gemini CLI as default.
