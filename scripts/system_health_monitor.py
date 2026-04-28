#!/usr/bin/env python3
"""
External system health monitor — runs OUTSIDE the super-agent/inspiring-cat containers.

Designed to be triggered by GitHub Actions daily cron or manually.
Has zero imports from app/ — it is fully standalone.

Checks:
  1. n8n API health + workflow statuses + last executions
  2. Claude CLI token healing layers (via DB + CLI worker health endpoint)
  3. Super-agent API health + CLI worker health
  4. Generates findings & improvement suggestions
  5. Stores results in PostgreSQL (monitoring_snapshots + monitoring_suggestions)
  6. Optionally forwards daily digest to Chief of Staff n8n workflow
  7. Prints coloured report to stdout (CI-friendly)

Environment variables required:
  DATABASE_URL          — PostgreSQL connection string
  N8N_BASE_URL          — n8n instance URL (e.g. https://n8n.up.railway.app)
  N8N_API_KEY           — n8n API key
  CLI_WORKER_URL        — inspiring-cat CLI worker URL
  SUPER_AGENT_URL       — super-agent main API URL
  CHIEF_OF_STAFF_WF_ID  — (optional) n8n workflow ID to ping with digest
  GITHUB_TOKEN          — (optional) for checking GH Actions oauth_refresh runs
  GITHUB_REPO           — (optional) e.g. gelson12/super-agent
  RUN_TYPE              — 'scheduled' | 'manual' (default: scheduled)
"""
from __future__ import annotations
import json
import os
import sys
import time
import uuid
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ── stdlib HTTP (no httpx dependency for portability) ─────────────────────────
import urllib.request
import urllib.error

# ── optional: psycopg2 ────────────────────────────────────────────────────────
try:
    import psycopg2
    HAS_PG = True
except ImportError:
    HAS_PG = False
    print("WARNING: psycopg2 not installed — results will NOT be saved to DB")

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")
if NO_COLOR:
    GREEN = RED = YELLOW = CYAN = DIM = BOLD = RESET = ""


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    category: str = "general"


@dataclass
class Suggestion:
    target_system: str
    severity: str          # critical | high | medium | low
    title: str
    description: str
    proposed_fix: str = ""


@dataclass
class RunState:
    checks: list[CheckResult] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def ok(self, name: str, detail: str = "", category: str = "general") -> None:
        self.checks.append(CheckResult(name, True, detail, category))
        _sym = f"{GREEN}✓{RESET}"
        print(f"  {_sym}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

    def fail(self, name: str, detail: str = "", category: str = "general") -> None:
        self.checks.append(CheckResult(name, False, detail, category))
        _sym = f"{RED}✗{RESET}"
        print(f"  {_sym}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

    def warn(self, name: str, detail: str = "", category: str = "general") -> None:
        self.checks.append(CheckResult(name, True, detail, category))
        _sym = f"{YELLOW}⚠{RESET}"
        print(f"  {_sym}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

    def suggest(self, s: Suggestion) -> None:
        self.suggestions.append(s)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def overall_status(self) -> str:
        if self.failed == 0:
            return "healthy"
        if self.failed <= 2:
            return "degraded"
        return "critical"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, headers: dict | None = None, timeout: int = 10) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body
    except Exception as e:
        return 0, str(e)


def _post(url: str, payload: dict, headers: dict | None = None, timeout: int = 10) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


DB_URL         = _env("DATABASE_URL").replace("postgres://", "postgresql://", 1)
N8N_URL        = _env("N8N_BASE_URL").rstrip("/")
N8N_KEY        = _env("N8N_API_KEY")
CLI_URL        = _env("CLI_WORKER_URL").rstrip("/")
AGENT_URL      = _env("SUPER_AGENT_URL").rstrip("/")
COS_WF_ID      = _env("CHIEF_OF_STAFF_WF_ID")
GH_TOKEN       = _env("GITHUB_TOKEN")
GH_REPO        = _env("GITHUB_REPO")
RUN_TYPE       = _env("RUN_TYPE", "scheduled")

# Verification monitor workflow ID (known fixed ID from test_recovery_layers.py)
VERIF_WF_ID    = "jun8CaMnNhux1iEY"

N8N_HEADERS    = {"X-N8N-API-KEY": N8N_KEY, "Accept": "application/json"}
GH_HEADERS     = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def _section(title: str) -> None:
    print(f"\n{CYAN}{BOLD}{'━'*62}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{'━'*62}{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# Check 1: n8n health + workflow audit
# ─────────────────────────────────────────────────────────────────────────────

def check_n8n(state: RunState) -> None:
    _section("n8n Health & Workflow Audit")
    state.raw["n8n"] = {}

    if not N8N_URL:
        state.fail("N8N_BASE_URL configured", "NOT SET", "n8n")
        state.suggest(Suggestion(
            "n8n", "critical", "N8N_BASE_URL not set",
            "The monitor cannot reach n8n — N8N_BASE_URL env var is missing.",
            "Set N8N_BASE_URL in GitHub Actions secrets and Railway Variables."
        ))
        return

    # 1a. Health endpoint
    code, body = _get(f"{N8N_URL}/healthz", timeout=8)
    if code == 200:
        state.ok("n8n /healthz", f"HTTP {code}", "n8n")
    else:
        state.fail("n8n /healthz", f"HTTP {code} — {str(body)[:120]}", "n8n")
        state.suggest(Suggestion(
            "n8n", "critical", "n8n health endpoint failing",
            f"/healthz returned HTTP {code}. n8n may be down or restarting.",
            "Check Railway n8n service logs. Redeploy if stuck."
        ))

    # 1b. List all workflows
    code, body = _get(f"{N8N_URL}/api/v1/workflows?limit=250", N8N_HEADERS, timeout=15)
    if code != 200 or not isinstance(body, dict):
        state.fail("n8n workflow list", f"HTTP {code}", "n8n")
        return

    workflows = body.get("data", [])
    state.ok("n8n workflow list", f"{len(workflows)} workflows found", "n8n")
    state.raw["n8n"]["workflows"] = [
        {"id": w["id"], "name": w["name"], "active": w.get("active", False)}
        for w in workflows
    ]

    # 1c. Flag inactive scheduled workflows (name hints at scheduling)
    schedule_keywords = ["daily", "monitor", "report", "keeper", "cron", "digest"]
    inactive_scheduled = [
        w for w in workflows
        if not w.get("active")
        and any(k in w["name"].lower() for k in schedule_keywords)
    ]
    if inactive_scheduled:
        names = ", ".join(w["name"] for w in inactive_scheduled[:5])
        state.fail("Scheduled workflows all active", f"INACTIVE: {names}", "n8n")
        state.suggest(Suggestion(
            "n8n", "high", f"Scheduled workflows are inactive: {names}",
            "These workflows appear to be scheduled but are currently disabled. "
            "They won't run on their triggers until activated.",
            "Activate each workflow in the n8n UI or via n8n API: "
            f"POST /api/v1/workflows/<id>/activate for IDs: "
            f"{', '.join(w['id'] for w in inactive_scheduled[:5])}"
        ))
    else:
        state.ok("Scheduled workflows all active", "", "n8n")

    # 1d. Verification monitor active
    verif_wf = next((w for w in workflows if w["id"] == VERIF_WF_ID or
                     "verification" in w["name"].lower()), None)
    if verif_wf:
        if verif_wf.get("active"):
            state.ok("Claude Verification Monitor active", verif_wf["name"], "n8n")
        else:
            state.fail("Claude Verification Monitor active",
                       f"'{verif_wf['name']}' is INACTIVE — Layer 4 recovery is BROKEN", "n8n")
            state.suggest(Suggestion(
                "n8n", "critical", "Claude Verification Monitor is inactive",
                "This workflow receives the magic link code during Layer 4 CLI recovery. "
                "When inactive, any full token recovery attempt will time out after 360s.",
                f"Activate workflow ID {verif_wf['id']} in n8n immediately."
            ))
    else:
        state.fail("Claude Verification Monitor found", "NOT FOUND in workflow list", "n8n")
        state.suggest(Suggestion(
            "n8n", "critical", "Claude Verification Monitor missing from n8n",
            "The workflow that intercepts the magic link email for CLI token recovery "
            "does not exist. Layer 4 recovery cannot function.",
            "Re-deploy super-agent — the post-deploy check auto-creates this workflow "
            "from n8n/claude_verification_monitor.json."
        ))

    # 1e. Check last executions for key workflows
    key_names = ["chief", "daily", "crypto", "finance", "business hub", "superagent chat"]
    key_wfs = [w for w in workflows if any(k in w["name"].lower() for k in key_names)]

    now_ts = time.time()
    for wf in key_wfs[:8]:
        code2, exec_body = _get(
            f"{N8N_URL}/api/v1/executions?workflowId={wf['id']}&limit=1",
            N8N_HEADERS, timeout=10
        )
        if code2 != 200 or not isinstance(exec_body, dict):
            state.warn(f"Executions for '{wf['name']}'", f"HTTP {code2}", "n8n")
            continue
        execs = exec_body.get("data", [])
        if not execs:
            state.warn(f"'{wf['name']}' last run", "no executions recorded", "n8n")
            continue
        last = execs[0]
        status = last.get("status", "unknown")
        started = last.get("startedAt", "")
        age_str = ""
        if started:
            try:
                ts = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
                age_h = (now_ts - ts) / 3600
                age_str = f"{age_h:.1f}h ago"
            except Exception:
                age_str = started[:16]

        if status == "success":
            state.ok(f"'{wf['name']}' last run", f"{status} {age_str}", "n8n")
        elif status == "error":
            state.fail(f"'{wf['name']}' last run", f"FAILED {age_str}", "n8n")
            state.suggest(Suggestion(
                "n8n", "high", f"Workflow '{wf['name']}' last execution failed",
                f"The most recent execution of '{wf['name']}' (ID {wf['id']}) ended in error {age_str}.",
                f"Inspect the failed execution in the n8n UI for workflow {wf['id']}. "
                "Check for missing credentials, changed API schemas, or node errors."
            ))
        else:
            state.warn(f"'{wf['name']}' last run", f"{status} {age_str}", "n8n")

    state.raw["n8n"]["inactive_scheduled"] = [w["name"] for w in inactive_scheduled]


# ─────────────────────────────────────────────────────────────────────────────
# Check 2: CLI token healing layers (external perspective)
# ─────────────────────────────────────────────────────────────────────────────

def check_cli_layers(state: RunState) -> None:
    _section("Claude CLI Token Healing Layers")
    state.raw["cli"] = {}

    # L1: Cloudflare block — expected, just note it
    state.warn("Layer 1 (Direct OAuth)", "Always blocked by Cloudflare on Railway IPs — expected", "cli")

    # L2a: CLI worker health endpoint → claude_available flag
    if not CLI_URL:
        state.fail("CLI Worker URL configured", "CLI_WORKER_URL not set", "cli")
        state.suggest(Suggestion(
            "cli_healing", "critical", "CLI_WORKER_URL not set in monitor",
            "Cannot probe Layer 2 CLI worker health. Add CLI_WORKER_URL to GitHub Actions secrets.",
            "Set CLI_WORKER_URL = https://cli-worker-production.up.railway.app (or Railway domain)"
        ))
    else:
        code, body = _get(f"{CLI_URL}/health", timeout=12)
        state.raw["cli"]["worker_health"] = body if isinstance(body, dict) else {}
        if code == 200 and isinstance(body, dict):
            claude_ok = body.get("claude_available", False)
            gemini_ok = body.get("gemini_available", False)
            pending   = body.get("pending_tasks", 0)
            if claude_ok:
                state.ok("Layer 2 CLI worker (claude_available)", f"pending_tasks={pending}", "cli")
            else:
                state.fail("Layer 2 CLI worker (claude_available)",
                           f"claude_available=False — CLI is DOWN, pending_tasks={pending}", "cli")
                state.suggest(Suggestion(
                    "cli_healing", "critical", "CLI worker reports claude_available=False",
                    "The inspiring-cat CLI worker cannot execute Claude Pro CLI calls. "
                    "This means all Pro-tier routing is broken and the system is falling back "
                    "to the API key.",
                    "Check inspiring-cat Railway logs. Trigger Layer 4 recovery via "
                    "POST /recover/cli on super-agent, or manually via the n8n workflow."
                ))
            if not gemini_ok:
                state.warn("Gemini CLI available", "gemini_available=False", "cli")
        else:
            state.fail("CLI worker /health reachable", f"HTTP {code}", "cli")
            state.suggest(Suggestion(
                "cli_healing", "high", "CLI worker health endpoint unreachable",
                f"GET {CLI_URL}/health returned HTTP {code}. "
                "The inspiring-cat container may be down or restarting.",
                "Check Railway inspiring-cat service status. If crash-looping, "
                "check entrypoint.cli.sh logs for Playwright or credential restore errors."
            ))

    # L2b: PostgreSQL credential record
    if not DB_URL:
        state.fail("DATABASE_URL configured", "NOT SET", "cli")
    else:
        try:
            import psycopg2 as _pg
            with _pg.connect(DB_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT expires_at, subscription_type, updated_at "
                        "FROM claude_credentials WHERE id='primary'"
                    )
                    row = cur.fetchone()
            if row:
                exp_ms, sub, updated = row
                if exp_ms:
                    rem_s = int((exp_ms - time.time() * 1000) / 1000)
                    if rem_s > 3600:
                        state.ok("Layer 2b PostgreSQL token",
                                 f"sub={sub or '?'} expires in {rem_s//3600}h {(rem_s%3600)//60}m", "cli")
                        state.raw["cli"]["token_expires_in_h"] = round(rem_s / 3600, 1)
                    elif rem_s > 0:
                        state.warn("Layer 2b PostgreSQL token",
                                   f"EXPIRING SOON — {rem_s//60}m remaining, sub={sub}", "cli")
                        state.suggest(Suggestion(
                            "cli_healing", "high", "Claude token expiring within 1 hour",
                            f"The PostgreSQL credential backup expires in {rem_s//60} minutes. "
                            "If the pro_token_keeper APScheduler job has stopped, the token won't auto-refresh.",
                            "Check inspiring-cat logs for pro_token_keeper scheduler errors. "
                            "If needed, force-run the keeper via CLI: POST /tasks {type: claude_auth}."
                        ))
                    else:
                        state.fail("Layer 2b PostgreSQL token",
                                   f"EXPIRED {abs(rem_s)//3600}h ago — Layer 2b is stale", "cli")
                        state.suggest(Suggestion(
                            "cli_healing", "critical", "PostgreSQL token backup is expired",
                            f"The stored Claude credential has been expired for {abs(rem_s)//3600}h. "
                            "This means if the container restarts it cannot restore a valid token from DB.",
                            "Trigger the full Layer 4 recovery to obtain a fresh token. "
                            "POST /recover/cli on super-agent (requires alpha0 safe word)."
                        ))
                else:
                    state.ok("Layer 2b PostgreSQL token", "row exists (no expiry field)", "cli")
            else:
                state.fail("Layer 2b PostgreSQL token", "no 'primary' row — DB backup never written", "cli")
                state.suggest(Suggestion(
                    "cli_healing", "high", "claude_credentials table has no primary row",
                    "The CLI worker has never saved a token to PostgreSQL, or the row was deleted. "
                    "Layer 2b recovery is unavailable.",
                    "Run the CLI worker's _save_credentials_to_db() by sending a claude_auth task: "
                    "POST /tasks {type: claude_auth} to the CLI worker."
                ))
        except Exception as e:
            state.fail("Layer 2b PostgreSQL connection", str(e)[:120], "cli")

    # L3: GitHub Actions oauth_refresh
    if GH_TOKEN and GH_REPO:
        code, body = _get(
            f"https://api.github.com/repos/{GH_REPO}/actions/workflows/oauth_refresh.yml/runs?per_page=1",
            GH_HEADERS, timeout=10
        )
        if code == 200 and isinstance(body, dict):
            runs = body.get("workflow_runs", [])
            if runs:
                last_run = runs[0]
                conclusion = last_run.get("conclusion", "none")
                created_at = last_run.get("created_at", "")
                age_str = ""
                if created_at:
                    try:
                        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
                        age_h = (time.time() - ts) / 3600
                        age_str = f"{age_h:.1f}h ago"
                    except Exception:
                        age_str = created_at[:16]
                if conclusion == "success":
                    state.ok("Layer 3 GitHub Actions oauth_refresh", f"{conclusion} {age_str}", "cli")
                elif conclusion == "skipped":
                    state.warn("Layer 3 GitHub Actions oauth_refresh",
                               f"last run was skipped {age_str} — OAUTH_RELAY_SECRET may not be set", "cli")
                else:
                    state.fail("Layer 3 GitHub Actions oauth_refresh",
                               f"conclusion={conclusion} {age_str}", "cli")
            else:
                state.warn("Layer 3 GitHub Actions oauth_refresh", "no runs found — workflow may not exist", "cli")
        else:
            state.warn("Layer 3 GitHub Actions check", f"HTTP {code} — GH API unavailable or workflow missing", "cli")
    else:
        state.warn("Layer 3 GitHub Actions check", "GITHUB_TOKEN or GITHUB_REPO not set — skipped", "cli")

    # L4: n8n verification monitor (already checked in n8n section, just cross-ref)
    state.ok("Layer 4 Playwright+n8n", "see n8n section for verification monitor status", "cli")

    # Blocking file detection — can only confirm from inside container, note limitation
    state.warn("Blocking files (.claude_login_ratelimit, .recovery_in_progress.lock)",
               "Cannot inspect from outside container — run test_recovery_layers.py inside inspiring-cat for full check",
               "cli")


# ─────────────────────────────────────────────────────────────────────────────
# Check 3: Super-agent API health
# ─────────────────────────────────────────────────────────────────────────────

def check_super_agent(state: RunState) -> None:
    _section("Super-Agent API Health")
    state.raw["agent"] = {}

    if not AGENT_URL:
        state.warn("SUPER_AGENT_URL configured", "not set — skipping super-agent checks", "agent")
        return

    # Health
    code, body = _get(f"{AGENT_URL}/health", timeout=10)
    state.raw["agent"]["health"] = body if isinstance(body, dict) else {}
    if code == 200:
        state.ok("Super-agent /health", f"HTTP {code}", "agent")
    else:
        state.fail("Super-agent /health", f"HTTP {code}", "agent")
        state.suggest(Suggestion(
            "super_agent", "critical", "Super-agent /health endpoint not responding",
            f"GET {AGENT_URL}/health returned HTTP {code}. "
            "The main super-agent container may be down.",
            "Check Railway super-agent service logs and restart if necessary."
        ))

    # Credits
    code, body = _get(f"{AGENT_URL}/credits", timeout=10)
    if code == 200 and isinstance(body, dict):
        state.raw["agent"]["credits"] = body
        anthropic_ok = body.get("anthropic_api", {}).get("available", True)
        if not anthropic_ok:
            state.fail("Anthropic API credits", "credits exhausted or key invalid", "agent")
            state.suggest(Suggestion(
                "super_agent", "high", "Anthropic API credits exhausted",
                "The ANTHROPIC_API_KEY has run out of credits. "
                "This is the final fallback when CLI and Gemini are both unavailable.",
                "Top up credits at console.anthropic.com or rotate the API key."
            ))
        else:
            state.ok("Anthropic API credits", "available", "agent")
    else:
        state.warn("Super-agent /credits", f"HTTP {code}", "agent")

    # Stats
    code, body = _get(f"{AGENT_URL}/stats", timeout=10)
    if code == 200 and isinstance(body, dict):
        state.raw["agent"]["stats"] = body
        state.ok("Super-agent /stats", "reachable", "agent")
    else:
        state.warn("Super-agent /stats", f"HTTP {code}", "agent")


# ─────────────────────────────────────────────────────────────────────────────
# Check 4: n8n crypto/finance workflow deep audit
# ─────────────────────────────────────────────────────────────────────────────

def check_crypto_workflow(state: RunState) -> None:
    _section("Crypto / Finance Workflow Deep Audit")
    state.raw["crypto"] = {}

    if not N8N_URL or not N8N_KEY:
        state.warn("Crypto workflow check", "n8n not configured — skipping", "crypto")
        return

    # Find crypto/finance workflows
    code, body = _get(f"{N8N_URL}/api/v1/workflows?limit=250", N8N_HEADERS, timeout=15)
    if code != 200 or not isinstance(body, dict):
        state.fail("Crypto workflow list", f"HTTP {code}", "crypto")
        return

    workflows = body.get("data", [])
    crypto_keywords = ["crypto", "trading", "bitcoin", "ethereum", "coin", "market", "price"]
    finance_keywords = ["finance", "financial", "investment", "portfolio"]
    all_keywords = crypto_keywords + finance_keywords

    target_wfs = [
        w for w in workflows
        if any(k in w["name"].lower() for k in all_keywords)
    ]

    if not target_wfs:
        state.warn("Crypto/finance workflows found",
                   "None found by keyword — workflow may have a custom name. "
                   "Check n8n manually for crypto-related workflows.", "crypto")
        state.suggest(Suggestion(
            "crypto_workflow", "medium", "Crypto workflow not identifiable by name",
            "The health monitor couldn't find a workflow with 'crypto', 'trading', 'bitcoin', "
            "'finance' etc. in the name. Either the workflow was renamed or doesn't exist yet.",
            "Verify the crypto workflow exists in n8n and confirm its name. "
            "Update CRYPTO_WF_NAME env var if needed."
        ))
        return

    now_ts = time.time()
    for wf in target_wfs:
        is_active = wf.get("active", False)
        wf_name = wf["name"]
        wf_id = wf["id"]

        if not is_active:
            state.fail(f"'{wf_name}' is active", "INACTIVE", "crypto")
            state.suggest(Suggestion(
                "crypto_workflow", "high", f"Crypto workflow '{wf_name}' is inactive",
                f"Workflow ID {wf_id} '{wf_name}' is disabled and will not run on its schedule.",
                f"Activate it: POST {N8N_URL}/api/v1/workflows/{wf_id}/activate"
            ))
        else:
            state.ok(f"'{wf_name}' is active", "", "crypto")

        # Last 5 executions
        code2, exec_body = _get(
            f"{N8N_URL}/api/v1/executions?workflowId={wf_id}&limit=5",
            N8N_HEADERS, timeout=10
        )
        if code2 != 200 or not isinstance(exec_body, dict):
            state.warn(f"'{wf_name}' executions", f"HTTP {code2}", "crypto")
            continue

        execs = exec_body.get("data", [])
        if not execs:
            state.warn(f"'{wf_name}' execution history", "no executions found — never triggered?", "crypto")
            state.suggest(Suggestion(
                "crypto_workflow", "medium", f"'{wf_name}' has no execution history",
                "This workflow has never been triggered or all executions were pruned.",
                "Manually trigger the workflow once to verify it runs end-to-end."
            ))
            continue

        error_count = sum(1 for e in execs if e.get("status") == "error")
        success_count = sum(1 for e in execs if e.get("status") == "success")
        last_status = execs[0].get("status", "unknown")
        last_started = execs[0].get("startedAt", "")

        age_str = ""
        if last_started:
            try:
                ts = datetime.fromisoformat(last_started.replace("Z", "+00:00")).timestamp()
                age_h = (now_ts - ts) / 3600
                age_str = f"{age_h:.1f}h ago"
                # Stale check: scheduled workflow not run in 25h
                if is_active and age_h > 25:
                    state.fail(f"'{wf_name}' recently executed",
                               f"last run was {age_h:.1f}h ago — STALE for scheduled workflow", "crypto")
                    state.suggest(Suggestion(
                        "crypto_workflow", "high",
                        f"'{wf_name}' hasn't run in {age_h:.0f}h",
                        "The workflow is active but hasn't executed recently. "
                        "The cron trigger may be misconfigured or the workflow is silently failing to start.",
                        "Check the workflow trigger node in n8n. Re-save the workflow to re-register the cron."
                    ))
                else:
                    state.ok(f"'{wf_name}' recently executed", age_str, "crypto")
            except Exception:
                age_str = last_started[:16]

        if error_count > 0:
            state.fail(f"'{wf_name}' recent error rate",
                       f"{error_count}/{len(execs)} recent executions failed", "crypto")
            state.suggest(Suggestion(
                "crypto_workflow", "high",
                f"'{wf_name}' has {error_count}/{len(execs)} recent failures",
                f"Recent execution history shows errors. Last status: {last_status} {age_str}. "
                "This could indicate API rate limits, changed data schemas, or node misconfiguration.",
                f"Inspect failed executions in n8n UI for workflow {wf_id}. "
                "Look for HTTP 429 (rate limit), 401 (auth), or changed API response shapes."
            ))
        else:
            state.ok(f"'{wf_name}' error rate",
                     f"0/{len(execs)} failures in recent history", "crypto")

        state.raw["crypto"][wf_id] = {
            "name": wf_name, "active": is_active,
            "last_status": last_status, "error_count": error_count,
            "success_count": success_count
        }


# ─────────────────────────────────────────────────────────────────────────────
# Persist to PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────

def save_to_db(state: RunState, run_type: str) -> str | None:
    if not HAS_PG or not DB_URL:
        print(f"\n{YELLOW}⚠  Skipping DB save (psycopg2 not available or DATABASE_URL not set){RESET}")
        return None

    try:
        # Determine per-category status
        def cat_status(cat: str) -> str:
            cat_checks = [c for c in state.checks if c.category == cat]
            failures = sum(1 for c in cat_checks if not c.passed)
            if failures == 0:
                return "healthy"
            if failures <= 1:
                return "degraded"
            return "critical"

        snapshot_data = {
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail, "category": c.category}
                       for c in state.checks],
            "raw": state.raw,
        }

        import psycopg2 as _pg
        import psycopg2.extras
        with _pg.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO monitoring_snapshots
                        (run_type, overall_status, n8n_status, cli_status, agent_status,
                         checks_passed, checks_failed, checks_total, data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    run_type,
                    state.overall_status,
                    cat_status("n8n"),
                    cat_status("cli"),
                    cat_status("agent"),
                    state.passed,
                    state.failed,
                    len(state.checks),
                    json.dumps(snapshot_data),
                ))
                snapshot_id = str(cur.fetchone()[0])

                for s in state.suggestions:
                    cur.execute("""
                        INSERT INTO monitoring_suggestions
                            (snapshot_id, target_system, severity, title, description, proposed_fix)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        snapshot_id,
                        s.target_system,
                        s.severity,
                        s.title,
                        s.description,
                        s.proposed_fix,
                    ))
            conn.commit()
        print(f"\n{GREEN}✓  Saved snapshot {snapshot_id} to PostgreSQL{RESET}")
        print(f"{GREEN}✓  {len(state.suggestions)} suggestions stored{RESET}")
        return snapshot_id
    except Exception as e:
        print(f"\n{RED}✗  DB save failed: {e}{RESET}")
        traceback.print_exc()
        return None
# ---------------------------------------------------------------# Fallback: save to JSON artifact (for GH Actions runners that cannot reach DB)# ---------------------------------------------------------------def save_to_artifact(state: RunState, run_type: str) -> str | None:    """Save monitoring snapshot to a JSON file."""    try:        def cat_status(cat: str) -> str:            cat_checks = [c for c in state.checks if c.category == cat]            failures = sum(1 for c in cat_checks if not c.passed)            if failures == 0: return "healthy"            if failures <= 1: return "degraded"            return "critical"        snapshot = {            "run_id": str(uuid.uuid4())[:8],            "run_type": run_type,            "timestamp": datetime.now(timezone.utc).isoformat(),            "overall_status": state.overall_status,            "per_category": {                "n8n": cat_status("n8n"),                "cli": cat_status("cli"),                "agent": cat_status("agent"),                "crypto": cat_status("crypto"),            },            "checks_passed": state.passed,            "checks_failed": state.failed,            "checks_total": len(state.checks),            "suggestions_count": len(state.suggestions),            "critical_suggestions": sum(1 for s in state.suggestions if s.severity == "critical"),            "high_suggestions": sum(1 for s in state.suggestions if s.severity == "high"),            "top_suggestions": [                {"severity": s.severity, "system": s.target_system, "title": s.title}                for s in sorted(state.suggestions, key=lambda x: ["critical","high","medium","low"].index(x.severity))[:10]            ],        },        aid = snapshot["run_id"]        p = "/tmp/health_monitor_" + aid + ".json"        with open(p, "w") as f: json.dump(snapshot, f, indent=2, default=str)        print(f"{GREEN}Saved artifact to {p}{RESET}")        return p    except Exception as e:        print(f"{YELLOW}Artifact save failed: {e}{RESET}")        return None

# ─────────────────────────────────────────────────────────────────────────────
# Chief of Staff digest
# ─────────────────────────────────────────────────────────────────────────────

def notify_chief_of_staff(state: RunState, snapshot_id: str | None) -> None:
    if not N8N_URL or not COS_WF_ID:
        return

    critical = [s for s in state.suggestions if s.severity == "critical"]
    high = [s for s in state.suggestions if s.severity == "high"]

    digest = {
        "source": "system_health_monitor",
        "run_type": RUN_TYPE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id or "not-saved",
        "overall_status": state.overall_status,
        "checks_passed": state.passed,
        "checks_failed": state.failed,
        "critical_issues": len(critical),
        "high_issues": len(high),
        "top_issues": [
            {"severity": s.severity, "title": s.title, "system": s.target_system}
            for s in (critical + high)[:5]
        ],
        "dashboard_url": f"{AGENT_URL}/monitoring" if AGENT_URL else "not-set",
    }

    code, _ = _post(
        f"{N8N_URL}/api/v1/workflows/{COS_WF_ID}/run",
        {"data": digest},
        N8N_HEADERS,
        timeout=15,
    )
    if code in (200, 201):
        print(f"{GREEN}✓  Chief of Staff notified (workflow {COS_WF_ID}){RESET}")
    else:
        print(f"{YELLOW}⚠  Chief of Staff notification failed — HTTP {code}{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    started = time.time()
    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║  BRIDGE DIGITAL — SYSTEM HEALTH MONITOR                      ║{RESET}")
    print(f"{BOLD}{CYAN}║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  run_type={RUN_TYPE:<10}               ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════╝{RESET}")

    state = RunState()

    # Run all checks
    try:
        check_n8n(state)
    except Exception as e:
        state.fail("n8n check (unexpected error)", str(e), "n8n")

    try:
        check_cli_layers(state)
    except Exception as e:
        state.fail("CLI layers check (unexpected error)", str(e), "cli")

    try:
        check_super_agent(state)
    except Exception as e:
        state.fail("Super-agent check (unexpected error)", str(e), "agent")

    try:
        check_crypto_workflow(state)
    except Exception as e:
        state.fail("Crypto workflow check (unexpected error)", str(e), "crypto")

    # Summary
    _section("SUMMARY")
    elapsed = round(time.time() - started, 1)
    status_color = GREEN if state.overall_status == "healthy" else (YELLOW if state.overall_status == "degraded" else RED)
    print(f"  Overall: {status_color}{BOLD}{state.overall_status.upper()}{RESET}")
    print(f"  Checks:  {GREEN}{state.passed} passed{RESET} / {RED}{state.failed} failed{RESET} / {len(state.checks)} total")
    print(f"  Issues:  {len(state.suggestions)} suggestions generated")
    print(f"  Runtime: {elapsed}s")

    if state.suggestions:
        print(f"\n{BOLD}  Top suggestions:{RESET}")
        for s in sorted(state.suggestions, key=lambda x: ["critical","high","medium","low"].index(x.severity))[:5]:
            sev_color = RED if s.severity == "critical" else (YELLOW if s.severity == "high" else CYAN)
            print(f"  [{sev_color}{s.severity.upper()}{RESET}] {s.target_system}: {s.title}")

    # Persist (DB or artifact fallback)
    snapshot_id = save_to_db(state, RUN_TYPE)
    if snapshot_id is None:
        save_to_artifact(state, RUN_TYPE)

    # Notify Chief of Staff
    notify_chief_of_staff(state, snapshot_id)

    print(f"\n{DIM}  Full results visible at: {AGENT_URL}/monitoring (if SUPER_AGENT_URL is set){RESET}\n")
    # Always exit 0 -- health data was collected successfully.
    # Audit findings are informational, not crashes.
    return 0


if __name__ == "__main__":
    sys.exit(main())
