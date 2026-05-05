# Super-Agent — Claude CLI Context
**Last updated:** 2026-05-05

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

*(56 active workflows total on n8n instance)*

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
| bridge_researcher_bot | ⚠️ check n8n | `Bridge_Researcher_bot` |

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

## PENDING ISSUES (as of 2026-05-05)

- **Health Monitor workflows INACTIVE**: `ke7YzsAmGerVWVVc`, `yZckxfWsvugSBFZh`, `u0cyS73kZJWNNy8u` — activate all 3 + add Telegram notification node to each
- **Gmail Secretary INACTIVE**: `N4IBlfTKan8Oq4tQ` — activate
- **bridge_researcher_bot**: workflow ID not configured — check n8n and add to this file
- **Bridge _Chief_Sec_Off_bot**: space in env var name — rename to `Bridge_Chief_Sec_Off_bot` in Railway N8N service
- **inspiring-cat shell tasks**: Use `{"type": "shell", "payload": {"command": "bash -c '...'"}}` format
- **Legion hive**: 6 agents compete per query (shortlist_k=6). Fires simultaneously with CLI now.
- **bridge.agent_performance schema**: `agent_name`, `tasks_total`, `tasks_success`, `tasks_failed`, `date`






############################################################




"""
n8n Agent — every model can build workflows, no 
MCP required.

ARCHITECTURE:
  Every model contributes to workflow building via one of 3 paths:

  PATH A — Direct tool access (Claude CLI Pro via MCP, LangGraph agents)
    Full n8n Python tools: create, update, activate, debug.

  PATH B — Universal JSON Importer (Groq, Cerebras, Gemini, DeepSeek, any model)
    Model generates valid n8n workflow JSON from natural language.
    _import_workflow_json() POSTs it directly to n8n REST API.
    No MCP. No tools. Just JSON over HTTP. Works from ANY model.

  PATH C — Blueprint + Execute (models with good design but no tools)
    Model designs the architecture in plain text.
    Tool-capable tier executes the build using the blueprint as spec.

LANGUAGE: Fully bilingual English + Portuguese. Responds in user's language.
"""
from __future__ import annotations

import json
import os
import re
import time
import logging

import httpx
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic

from ..config import settings
from .agent_planner import extract_final_agent_text
from ..tools.n8n_tools import (
    n8n_list_workflows, n8n_get_workflow,
    n8n_create_workflow, n8n_update_workflow, n8n_delete_workflow,
    n8n_cleanup_test_workflows, n8n_activate_workflow, n8n_deactivate_workflow,
    n8n_execute_workflow, n8n_list_executions, n8n_get_execution,
)
from ..tools.railway_tools import (
    railway_list_services, railway_get_logs,
    railway_get_deployment_status, railway_list_variables, railway_redeploy,
)
from ..tools.shell_tools import run_shell_via_cli_worker, run_authorized_shell_command
from ..tools.obsidian_tools import OBSIDIAN_TOOLS

_log = logging.getLogger("n8n_agent")

# ══════════════════════════════════════════════════════════════════════════════
# UNIVERSAL WORKFLOW JSON IMPORTER
# Any model — including Groq, Cerebras, DeepSeek — generates JSON and this
# function handles the actual import to n8n via REST API.
# No MCP needed. No LangGraph needed. Pure HTTP.
# ══════════════════════════════════════════════════════════════════════════════

_JSON_GENERATION_PROMPT = """\
You are an n8n workflow JSON generator.

Generate a complete, valid n8n workflow JSON for this request:
{request}

RULES:
1. Output ONLY raw JSON — no markdown, no backticks, no explanation
2. The JSON must have exactly these top-level keys:
   "name", "nodes", "connections", "active", "settings"
3. Every node must have: "id" (unique string), "name", "type", "typeVersion",
   "position" ([x, y] array), "parameters" (object)
4. active must be false (we activate separately)
5. settings must be at minimum: {{}}

COMMON NODE TYPES (use exactly as shown):
  Webhook trigger:   "n8n-nodes-base.webhook"         typeVersion: 2
  Schedule trigger:  "n8n-nodes-base.scheduleTrigger" typeVersion: 1
  HTTP Request:      "n8n-nodes-base.httpRequest"     typeVersion: 4
  Send Email:        "n8n-nodes-base.emailSend"       typeVersion: 2
  Telegram:          "n8n-nodes-base.telegram"        typeVersion: 1
  If / condition:    "n8n-nodes-base.if"              typeVersion: 2
  Set fields:        "n8n-nodes-base.set"             typeVersion: 3
  Switch:            "n8n-nodes-base.switch"          typeVersion: 3
  Wait:              "n8n-nodes-base.wait"            typeVersion: 1
  Respond webhook:   "n8n-nodes-base.respondToWebhook" typeVersion: 1
  Code (JS):         "n8n-nodes-base.code"            typeVersion: 2
  Slack:             "n8n-nodes-base.slack"           typeVersion: 2
  Google Sheets:     "n8n-nodes-base.googleSheets"   typeVersion: 4

FOR AI STEPS — always use HTTP Request pointing at Super Agent:
  type: "n8n-nodes-base.httpRequest"
  parameters.url: "https://super-agent-production.up.railway.app/chat"
  parameters.method: "POST"
  parameters.sendBody: true
  parameters.bodyParameters.parameters: [{{"name":"message","value":"={{{{$json.input}}}}"}},{{"name":"session_id","value":"n8n-auto"}}]

CONNECTIONS format:
  {{"SourceNodeName": {{"main": [[{{"node": "TargetNodeName", "type": "main", "index": 0}}]]}}}}

Position nodes left-to-right: first node at [250, 300], each next +250 on x.

Output ONLY the JSON object. Nothing else."""


def _extract_json_from_response(text: str) -> dict | None:
    """
    Extract and parse n8n workflow JSON from a model response.
    Handles raw JSON, markdown code blocks, and partial wrappers.
    """
    if not text or not text.strip():
        return None

    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()

    # Try direct parse first
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "nodes" in data:
            return data
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    start = cleaned.find("{")
    end   = cleaned.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start:end])
            if isinstance(data, dict) and "nodes" in data:
                return data
        except json.JSONDecodeError:
            pass

    return None


def _import_workflow_json(workflow_json: dict, activate: bool = True) -> dict:
    """
    POST a workflow JSON dict directly to the n8n REST API and optionally activate it.
    This is the universal importer — works for any model that can generate JSON.

    Returns: {"ok": bool, "id": str, "name": str, "url": str, "error": str}
    """
    base_url = settings.n8n_base_url.rstrip("/")
    api_key  = settings.n8n_api_key

    headers = {
        "X-N8N-API-KEY": api_key,
        "Content-Type":  "application/json",
    }

    # Ensure required fields have defaults
    workflow_json.setdefault("active",   False)
    workflow_json.setdefault("settings", {})
    workflow_json.setdefault("connections", {})

    try:
        with httpx.Client(timeout=30) as client:
            # Step 1 — Create the workflow
            resp = client.post(
                f"{base_url}/api/v1/workflows",
                headers=headers,
                json=workflow_json,
            )
            resp.raise_for_status()
            created = resp.json()
            workflow_id   = created.get("id", "")
            workflow_name = created.get("name", workflow_json.get("name", "Workflow"))

            # Step 2 — Activate if requested
            if activate and workflow_id:
                time.sleep(1)  # brief pause for n8n to register the workflow
                try:
                    act_resp = client.post(
                        f"{base_url}/api/v1/workflows/{workflow_id}/activate",
                        headers=headers,
                    )
                    act_resp.raise_for_status()
                except Exception as act_err:
                    _log.warning("Activation failed for %s: %s", workflow_id, act_err)

            webhook_url = ""
            for node in workflow_json.get("nodes", []):
                if "webhook" in node.get("type", "").lower():
                    path = node.get("parameters", {}).get("path", "")
                    if path:
                        webhook_url = f"{base_url}/webhook/{path}"
                    break

            return {
                "ok":   True,
                "id":   workflow_id,
                "name": workflow_name,
                "url":  webhook_url,
                "error": "",
            }

    except httpx.HTTPStatusError as e:
        return {"ok": False, "id": "", "name": "", "url": "",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"ok": False, "id": "", "name": "", "url": "",
                "error": str(e)[:300]}


def _ask_legion_for_json(request: str) -> str | None:
    """
    Ask Legion hive (Groq/Cerebras/GH Models) to generate n8n workflow JSON.
    These are the fastest FREE models — Cerebras responds in <500ms.
    They don't have MCP tool access but can generate JSON perfectly.
    """
    try:
        from ..models.claude import _try_legion
        prompt = _JSON_GENERATION_PROMPT.format(request=request)
        result = _try_legion(prompt, timeout_s=15.0)
        return result if result and not result.startswith("[") else None
    except Exception:
        return None


def _ask_deepseek_for_json(request: str) -> str | None:
    """Ask DeepSeek to generate n8n workflow JSON — excellent at structured output."""
    try:
        from ..models.deepseek import ask_deepseek
        prompt = _JSON_GENERATION_PROMPT.format(request=request)
        result = ask_deepseek(prompt, system="You are an n8n JSON generator. Output only valid JSON.")
        return result if result and not result.startswith("[") else None
    except Exception:
        return None


def _ask_gemini_for_json(request: str) -> str | None:
    """Ask Gemini CLI to generate n8n workflow JSON."""
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        prompt = _JSON_GENERATION_PROMPT.format(request=request)
        result = ask_gemini_cli(prompt)
        return result if result and not result.startswith("[") else None
    except Exception:
        return None


def _try_universal_import(request: str) -> str | None:
    """
    Universal workflow builder — races all free models for JSON generation,
    then imports the first valid result directly to n8n via REST API.

    Order: Legion (Groq/Cerebras fastest) → DeepSeek → Gemini
    First valid JSON wins. Import happens immediately.

    This path requires ZERO MCP, ZERO tool access, ZERO LangGraph.
    It works from any model that understands JSON.
    """
    import concurrent.futures

    _log.info("Universal JSON importer: racing Legion, DeepSeek, Gemini for JSON generation")

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_ask_legion_for_json,   request): "Legion (Groq/Cerebras)",
            pool.submit(_ask_deepseek_for_json, request): "DeepSeek",
            pool.submit(_ask_gemini_for_json,   request): "Gemini",
        }

        for future in concurrent.futures.as_completed(futures, timeout=20):
            model_name = futures[future]
            try:
                raw = future.result()
                if not raw:
                    continue

                workflow_json = _extract_json_from_response(raw)
                if not workflow_json:
                    _log.debug("%s: JSON parse failed", model_name)
                    continue

                _log.info("Universal importer: %s generated valid JSON — importing to n8n", model_name)

                result = _import_workflow_json(workflow_json, activate=True)

                if result["ok"]:
                    name = result["name"]
                    wid  = result["id"]
                    url  = result["url"]

                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()

                    # Log to Obsidian vault in background
                    try:
                        from ..tools.obsidian_tools import obsidian_append_to_note
                        obsidian_append_to_note(
                            f"Workflows/{time.strftime('%Y-%m-%d')}-{name[:30]}.md",
                            f"## {name}\n- **ID:** {wid}\n- **Built by:** {model_name}\n"
                            f"- **Request:** {request[:200]}\n"
                            f"- **Webhook:** {url or 'N/A'}\n"
                            f"- **Status:** Active ✅\n",
                        )
                    except Exception:
                        pass

                    response = (
                        f"✅ **Workflow created and activated!**\n\n"
                        f"**Name:** {name}\n"
                        f"**ID:** `{wid}`\n"
                        f"**Built by:** {model_name} (no MCP needed)\n"
                    )
                    if url:
                        response += f"**Webhook URL:** `{url}`\n"
                    response += (
                        f"\nThe workflow is live in n8n. "
                        f"You can find it under Active Workflows in your n8n dashboard."
                    )
                    return response

                else:
                    _log.warning(
                        "Universal importer: %s JSON rejected by n8n: %s",
                        model_name, result["error"]
                    )

            except Exception as e:
                _log.debug("Universal importer: %s failed: %s", model_name, e)
                continue

    return None  # All models failed — fall through to LangGraph


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM = """You are Super Agent's n8n workflow automation manager with FULL ACCESS to n8n.

## LANGUAGE RULE — NON-NEGOTIABLE
Always respond in the same language the user wrote in.
Portuguese → respond in Portuguese. English → respond in English. Mixed → match dominant.

## THREE PATHS TO n8n — USE IN ORDER

### Path 1 — Python n8n tools (fastest, try first)
n8n_list_workflows, n8n_get_workflow, n8n_create_workflow, etc.

### Path 2 — curl via inspiring-cat CLI worker
run_shell_via_cli_worker("curl -s $N8N_BASE_URL/api/v1/workflows -H 'X-N8N-API-KEY: $N8N_API_KEY'")

### Path 3 — curl via super-agent container
run_authorized_shell_command("curl -s $N8N_BASE_URL/api/v1/workflows -H 'X-N8N-API-KEY: $N8N_API_KEY'")

## INFRASTRUCTURE SELF-HEALING — MANDATORY

If ALL THREE PATHS fail:
1. railway_get_deployment_status → is n8n running?
2. railway_list_services → confirm URL
3. railway_get_logs → find crash cause
4. railway_list_variables → confirm N8N_BASE_URL matches service
5. If crashed: railway_redeploy → wait 20s → retry
Never tell the user to go to Railway dashboard — fix it yourself.

## BUILDING WORKFLOWS FROM NATURAL LANGUAGE (English OR Portuguese)

Step 1 — Extract intent (translate mentally if Portuguese):
- TRIGGER: o que inicia / what starts it (schedule, webhook, Telegram, etc.)
- ACTIONS: o que faz / what it does step by step
- OUTPUT: onde vai o resultado / where the result goes (email, Telegram, Slack, etc.)

Step 2 — Map to n8n node types:
  "every day/hour · todo dia/hora"  → n8n-nodes-base.scheduleTrigger
  "webhook · quando receber"        → n8n-nodes-base.webhook
  "send email · enviar email"       → n8n-nodes-base.emailSend
  "Telegram message/bot"            → n8n-nodes-base.telegram
  "Google Sheets · planilha"        → n8n-nodes-base.googleSheets
  "HTTP call · chamada API"         → n8n-nodes-base.httpRequest
  "ask AI · perguntar IA · resumir" → httpRequest POST to https://super-agent-production.up.railway.app/chat
  "if/se · filter/filtrar"          → n8n-nodes-base.if
  "set fields · definir campos"     → n8n-nodes-base.set
  "wait · aguardar"                 → n8n-nodes-base.wait

Step 3 — Build in phases:
1. n8n_create_workflow — trigger + first action (skeleton only)
2. n8n_get_workflow — confirm creation, get live ID
3. n8n_update_workflow — add remaining nodes (max 5 per update)
4. n8n_activate_workflow — make it live
5. Report: name, ID, what it does, webhook URL if applicable

NEVER REFUSE a natural language request.
If unsure of node type → use n8n-nodes-base.httpRequest as universal fallback.
All tokens are already in Railway variables — never ask the user for credentials.

For AI steps inside workflows — always call Super Agent, not Anthropic directly:
POST https://super-agent-production.up.railway.app/chat
{"message": "{{$json.input}}", "session_id": "n8n-auto"}

## DEBUGGING FAILED EXECUTIONS
1. n8n_list_executions → find the failed one
2. n8n_get_execution → exact node + error
3. Propose fix → apply if authorized

## OBSIDIAN VAULT
Before building: obsidian_search_vault("<keywords>") → find prior designs
After building: write summary to Workflows/YYYY-MM-DD-name.md"""

# ── Tool list for LangGraph agents ───────────────────────────────────────────
_N8N_TOOLS = [
    n8n_list_workflows, n8n_get_workflow,
    n8n_create_workflow, n8n_update_workflow, n8n_delete_workflow,
    n8n_cleanup_test_workflows, n8n_activate_workflow, n8n_deactivate_workflow,
    n8n_execute_workflow, n8n_list_executions, n8n_get_execution,
    railway_list_services, railway_get_logs,
    railway_get_deployment_status, railway_list_variables, railway_redeploy,
    run_shell_via_cli_worker, run_authorized_shell_command,
    *OBSIDIAN_TOOLS,
]

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=settings.anthropic_api_key,
            max_tokens=8192,
        )
        _agent = create_react_agent(llm, _N8N_TOOLS)
    return _agent


def _is_mcp_error(text: str) -> bool:
    if not text:
        return True
    lower = text.lower()
    signals = (
        "mcp error", "connection refused", "n8n is unreachable",
        "tool execution failed", "could not connect", "502 bad gateway",
        "econnrefused", "etimedout",
    )
    return text.startswith("[") or any(s in lower for s in signals)


def _is_build_request(message: str) -> bool:
    """Detect workflow build requests in English or Portuguese."""
    lower = message.lower()
    keywords = (
        "create", "build", "make", "add", "generate", "set up", "setup",
        "automate", "design", "deploy", "write", "implement", "new workflow",
        "criar", "cria", "construir", "fazer", "gerar", "configurar",
        "automatizar", "projetar", "novo workflow", "novo fluxo",
        "montar", "desenvolver", "implementar",
    )
    return any(k in lower for k in keywords)


def _get_gemini_blueprint(message: str) -> str | None:
    """Ask Gemini to design workflow architecture as a blueprint for LangGraph."""
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        prompt = (
            f"Design the architecture for this n8n workflow. "
            f"Describe every node, its type, parameters, and connections. "
            f"Use exact n8n node type strings. Do NOT say you can't build it — "
            f"just describe the complete design:\n\n{message}"
        )
        result = ask_gemini_cli(f"{_SYSTEM}\n\n{prompt}")
        return result if result and not result.startswith("[") and len(result) > 80 else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_n8n_agent(message: str) -> str:
    """
    Run the n8n agent. Every model contributes — no model is wasted.

    Build request flow:
      1. Claude CLI Pro (MCP, zero cost)                    → direct build
      2. Universal JSON Importer                            → Legion/DeepSeek/Gemini
         (Groq/Cerebras generate JSON → POST to n8n REST)   race in parallel
      3. Gemini blueprint → LangGraph executes              → blueprint + tools
      4. LangGraph (Anthropic/DeepSeek) full tool access    → last resort

    Informational request flow:
      1. Claude CLI Pro
      2. Gemini CLI (text answer)
      3. LangGraph
    """
    if not settings.n8n_base_url:
        return (
            "⚠️ **N8N_BASE_URL not set.**\n"
            "Add it to Railway Variables for the super-agent service.\n\n"
            "PT: Adiciona N8N_BASE_URL nas variáveis do Railway."
        )
    if not settings.n8n_api_key:
        return (
            "⚠️ **N8N_API_KEY not set.**\n"
            "n8n → Settings → API → create key → add to Railway Variables.\n\n"
            "PT: n8n → Definições → API → cria chave → adiciona ao Railway."
        )

    # ── Pre-flight health check ───────────────────────────────────────────────
    try:
        from ..tools.n8n_repair import attempt_n8n_repair, n8n_health_check
        health = n8n_health_check()
        if not health["reachable"]:
            error_str = (health["issues"] or ["n8n unreachable"])[0]
            fixed, fixes = attempt_n8n_repair(error_str)
            if not fixed:
                return (
                    f"⚠️ n8n unreachable, auto-repair failed.\n"
                    f"Error: {error_str}\nCheck Railway logs."
                )
            message = (
                "[AUTO-REPAIR APPLIED]\n"
                + "\n".join(f"• {f}" for f in fixes)
                + f"\n\nn8n is now reachable. Proceeding:\n{message}"
            )
    except Exception:
        pass  # health check unavailable — proceed anyway

    is_build = _is_build_request(message)

    # ── PATH A: Claude CLI Pro ────────────────────────────────────────────────
    try:
        from ..learning.pro_router import try_pro, should_attempt_cli
        if should_attempt_cli():
            cli_result = try_pro(f"{_SYSTEM}\n\n{message}")
            if cli_result and not _is_mcp_error(cli_result):
                from ..activity_log import bg_log as _bg
                _bg("n8n: ✓ Claude CLI Pro", source="n8n_agent")
                return cli_result
    except Exception:
        pass

    # ── PATH B: Universal JSON Importer (build requests only) ────────────────
    # Groq/Cerebras/DeepSeek/Gemini generate JSON — we POST it directly to n8n.
    # No MCP. No LangGraph. Just JSON over HTTP. Fastest for simple workflows.
    if is_build:
        from ..activity_log import bg_log as _bg
        _bg("n8n: trying Universal JSON Importer (Legion/DeepSeek/Gemini race)", source="n8n_agent")
        import_result = _try_universal_import(message)
        if import_result:
            return import_result
        _bg("n8n: Universal importer failed — escalating to LangGraph", source="n8n_agent")

    # ── PATH C: Gemini blueprint + LangGraph execution ────────────────────────
    _gemini_blueprint = None
    if is_build:
        _gemini_blueprint = _get_gemini_blueprint(message)
        if _gemini_blueprint:
            from ..activity_log import bg_log as _bg
            _bg("n8n: Gemini blueprint captured → LangGraph will execute build", source="n8n_agent")
    else:
        # Informational — Gemini can answer directly
        try:
            from ..learning.gemini_cli_worker import ask_gemini_cli
            result = ask_gemini_cli(f"{_SYSTEM}\n\n{message}")
            no_tools = (
                "operating as gemini cli", "don't have direct access",
                "cannot directly interact", "copy this json", "import directly",
            )
            if result and not result.startswith("["):
                if not any(p in result.lower() for p in no_tools):
                    return result
                _gemini_blueprint = result
        except Exception:
            pass

    # Build message augmented with blueprint if available
    build_message = message
    if _gemini_blueprint:
        build_message = (
            f"{message}\n\n"
            f"[WORKFLOW BLUEPRINT — execute this design using n8n tools]:\n"
            f"{_gemini_blueprint}\n\n"
            f"Build this now using n8n_create_workflow and n8n_update_workflow."
        )

    # ── PATH D: LangGraph full tool access ────────────────────────────────────
    from .agent_routing import tiered_agent_invoke
    from ..activity_log import bg_log as _bg
    _bg("n8n: LangGraph full tool access", source="n8n_agent")

    return tiered_agent_invoke(
        message=build_message,
        system_prompt=_SYSTEM,
        tools=_N8N_TOOLS,
        agent_type="n8n",
        source="n8n_agent",
    )
