# Super-Agent — Gemini CLI Context

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

---

## N8N ACTIVE WORKFLOWS

| ID | Name |
|----|------|
| `jxnZZwTqJ7naPKc6` | Claude-Verification-Monitor (ACTIVE) |
| `ke7YzsAmGerVWVVc` | Super-Agent-Health-Monitor (ACTIVE) |
| `sCHZhoyRgEZUaxtT` | Universal Catch-All (ACTIVE) |

---

## COMMON PATTERNS

- **Routing miss** → add keyword to appropriate `_*_KEYWORDS` set in `dispatcher.py`
- **Website change** → github_agent reads `website/index.html`, updates ALL occurrences, commits + pushes
- **n8n task** → try Python tools first, then `run_shell_via_cli_worker` curl, then `run_authorized_shell_command` curl
- **Claude CLI DOWN** → wait up to 15 min for 4-layer self-healing watchdog

---

## PENDING ISSUES (2026-04-13)

- Anthropic API has NO CREDITS — Haiku/Sonnet/Opus API calls will fail until topped up at console.anthropic.com
- Verify shell task type end-to-end after inspiring-cat redeploy
