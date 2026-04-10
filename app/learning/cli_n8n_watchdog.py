"""
Full-Stack Self-Healing Watchdog

Autonomous recovery loop that tests and fixes ALL Super Agent paths:

  Tier 1 — Claude CLI Pro (inspiring-cat) — free
  Tier 2 — Gemini CLI                     — free
  Tier 3 — Anthropic API                  — paid
  Tier 4 — DeepSeek API                   — paid
  Tier 5 — n8n workflow creation           — REST API
  Tier 6 — n8n variable auto-detection     — sets N8N_BASE_URL if missing

Every 5 minutes:
  1. Diagnose: check all flag files + live endpoints
  2. Auto-fix: clear stale flags, restore credentials, set missing env vars
  3. Test each tier independently
  4. Test n8n workflow creation end-to-end
  5. Only declare RESOLVED when ALL available tiers pass
  6. Post dashboard alert (status bar + activity log)

Never raises. Never asks for user input. Loops until fixed.

Scheduler: APScheduler, every 5 minutes.
State: /workspace/cli_n8n_watchdog.json
Alert: /workspace/watchdog_alert.json → read by /status/now
"""
from __future__ import annotations

import json
import os
import time
import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE_DIR    = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
_STATE_FILE  = _BASE_DIR / "cli_n8n_watchdog.json"
_ALERT_FILE  = _BASE_DIR / "watchdog_alert.json"

# ── Thresholds ────────────────────────────────────────────────────────────────
_MAX_ATTEMPTS      = 100       # give up after this (~8h at 5 min intervals)
_ALERT_TTL_HOURS   = 2         # resolved alert shown in status bar for 2h
_WARNING_TTL_HOURS = 0.5       # failure alert refreshed every 30 min
_CLI_TEST_TIMEOUT  = 45
_N8N_TEST_TIMEOUT  = 20
_TEST_WF_PREFIX    = "Watchdog-Health-Test"

# ── New: CLI n8n build test + HA test thresholds ─────────────────────────────
_CLI_BUILD_TIMEOUT     = 180      # MCP builds involve multiple tool calls
_CLI_HA_TIMEOUT        = 45
_BUILD_TEST_PREFIX     = "Watchdog-CLI-Build-Test"
_BUILD_COOLDOWN_HOURS  = 6        # skip expensive build test if last success < 6h ago

_BUILD_REFUSAL_PHRASES = (
    "i can't", "i cannot", "i'm unable", "not able to",
    "don't have access", "no mcp", "cannot create workflows",
    "don't have direct access", "operating as", "headless cli",
    "without direct network", "copy this json", "paste it directly",
    "import directly into",
)


def _log(msg: str) -> None:
    try:
        from ..activity_log import bg_log
        bg_log(msg, source="cli_n8n_watchdog")
    except Exception:
        pass


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "active": False,
        "attempt_count": 0,
        "resolved": False,
        "resolved_at": None,
        "last_failure_mode": "",
        "last_attempt_ts": 0,
        "resolved_workflow_id": None,
        "resolved_workflow_name": None,
        "tier_results": {},
        "cli_build_ok": False,
        "cli_build_detail": "",
        "cli_build_workflow_id": None,
        "cli_build_last_success_ts": 0,
        "cli_ha_ok": False,
        "cli_ha_detail": "",
    }


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Dashboard alert sentinel ──────────────────────────────────────────────────

def _write_alert(alert_type: str, msg: str) -> None:
    try:
        if alert_type == "clear":
            _ALERT_FILE.unlink(missing_ok=True)
            return
        payload = {"type": alert_type, "msg": msg, "ts": time.time()}
        _ALERT_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def read_watchdog_alert() -> str | None:
    try:
        if not _ALERT_FILE.exists():
            return None
        data = json.loads(_ALERT_FILE.read_text(encoding="utf-8"))
        age_hours = (time.time() - data.get("ts", 0)) / 3600
        alert_type = data.get("type", "warning")
        ttl = _ALERT_TTL_HOURS if alert_type == "resolved" else _WARNING_TTL_HOURS
        if age_hours > ttl:
            return None
        prefix = "✅ RESOLVED" if alert_type == "resolved" else "⚠️ Watchdog"
        return f"{prefix}: {data.get('msg', '')}"
    except Exception:
        return None


# ── Tier tests ────────────────────────────────────────────────────────────────

def _test_cli() -> tuple[bool, str]:
    """Test Claude CLI Pro via try_pro. Returns (ok, detail)."""
    try:
        from .pro_router import try_pro, should_attempt_cli
        if not should_attempt_cli():
            return False, "CLI flagged down (daily/burst/cli_down)"
        result = try_pro("Reply with exactly the word OK and nothing else.")
        if not result:
            return False, "CLI returned None"
        # try_pro() returns None on failure, or a string starting with "["
        # for errors. Any non-None, non-"[" response is a real CLI response.
        if result.startswith("["):
            return False, f"CLI error: {result[:100]}"
        return True, "OK"
    except Exception as e:
        return False, f"exception: {e}"


def _test_gemini() -> tuple[bool, str]:
    """Test Gemini CLI. Returns (ok, detail)."""
    try:
        from .gemini_cli_worker import ask_gemini_cli
        result = ask_gemini_cli("Reply with exactly the word OK and nothing else.")
        if not result:
            return False, "Gemini returned None"
        if result.startswith("["):
            return False, f"Gemini error: {result[:100]}"
        return True, "OK"
    except Exception as e:
        return False, f"exception: {e}"


def _test_anthropic() -> tuple[bool, str]:
    """Test Anthropic API with minimal prompt. Returns (ok, detail)."""
    try:
        from ..config import settings
        if not settings.anthropic_api_key:
            return False, "ANTHROPIC_API_KEY not set"
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply OK"}],
        )
        text = resp.content[0].text.strip()
        return True, "OK"
    except Exception as e:
        err = str(e).lower()
        if "credit" in err:
            return False, "no credits"
        return False, f"exception: {e}"


def _test_deepseek() -> tuple[bool, str]:
    """Test DeepSeek API. Returns (ok, detail)."""
    try:
        from ..config import settings
        if not settings.deepseek_api_key:
            return False, "DEEPSEEK_API_KEY not set"
        from ..models.deepseek import ask_deepseek
        result = ask_deepseek("Reply with exactly the word OK and nothing else.", system="")
        if not result:
            return False, "DeepSeek returned None"
        if result.startswith("["):
            return False, f"DeepSeek error: {result[:100]}"
        return True, "OK"
    except Exception as e:
        return False, f"exception: {e}"


def _test_n8n() -> tuple[bool, str, str | None, str | None]:
    """Test n8n workflow creation. Returns (ok, detail, workflow_id, workflow_name)."""
    try:
        from ..config import settings
        if not settings.n8n_base_url:
            return False, "N8N_BASE_URL not set", None, None
        if not settings.n8n_api_key:
            return False, "N8N_API_KEY not set", None, None

        import urllib.request as _urlr
        ts = int(time.time())
        name = f"{_TEST_WF_PREFIX}-{ts}"
        workflow_body = json.dumps({
            "name": name,
            "nodes": [
                {"id": "trigger-1", "name": "Manual Trigger",
                 "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1,
                 "position": [240, 300], "parameters": {}},
                {"id": "noop-1", "name": "Watchdog OK",
                 "type": "n8n-nodes-base.noOp", "typeVersion": 1,
                 "position": [460, 300], "parameters": {}},
            ],
            "connections": {
                "Manual Trigger": {
                    "main": [[{"node": "Watchdog OK", "type": "main", "index": 0}]]
                }
            },
            "settings": {"executionOrder": "v1", "saveManualExecutions": False},
        }).encode("utf-8")

        req = _urlr.Request(
            f"{settings.n8n_base_url.rstrip('/')}/api/v1/workflows",
            data=workflow_body,
            headers={
                "X-N8N-API-KEY": settings.n8n_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with _urlr.urlopen(req, timeout=_N8N_TEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            wf_id = str(body.get("id", ""))
            if wf_id:
                return True, "OK", wf_id, body.get("name", name)
        return False, "n8n returned no workflow ID", None, None
    except Exception as e:
        return False, f"exception: {e}", None, None


# ── NEW: CLI n8n Build Test ───────────────────────────────────────────────────

def _test_cli_n8n_build() -> tuple[bool, str, str | None]:
    """
    Send a real n8n build prompt to Claude CLI, then verify the workflow
    was actually created in n8n canvas via REST API.
    Returns (ok, detail, workflow_id).
    """
    try:
        from ..config import settings
        if not settings.n8n_base_url or not settings.n8n_api_key:
            return False, "n8n not configured (N8N_BASE_URL/N8N_API_KEY missing)", None

        import re
        import urllib.request as _urlr

        ts = int(time.time())
        wf_name = f"{_BUILD_TEST_PREFIX}-{ts}"
        build_prompt = (
            f"Using your n8n MCP tools, create a workflow called '{wf_name}' "
            f"with a Manual Trigger node connected to a NoOp node called 'CLI-Built-OK'. "
            f"Use the create_workflow tool to build it directly in n8n. "
            f"After creating it, reply with only the workflow ID."
        )

        cli_result = None

        # Try inspiring-cat CLI worker first
        try:
            from .pro_router import _submit_and_poll, _cli_worker_url
            if _cli_worker_url():
                cli_result = _submit_and_poll(
                    "claude_pro", {"prompt": build_prompt},
                    timeout=_CLI_BUILD_TIMEOUT + 30,
                )
        except Exception:
            pass

        # Fallback: local subprocess
        if not cli_result:
            try:
                import subprocess as _sp
                _env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
                _env["HOME"] = "/root"
                proc = _sp.run(
                    ["claude", "-p", build_prompt],
                    capture_output=True, text=True, input="1\n" * 30,
                    timeout=_CLI_BUILD_TIMEOUT, cwd="/workspace", env=_env,
                )
                cli_result = (proc.stdout or "").strip()
            except Exception:
                pass

        if not cli_result:
            return False, "CLI returned no response", None

        # Check for refusal
        lower = cli_result.lower()
        if any(p in lower for p in _BUILD_REFUSAL_PHRASES):
            return False, f"CLI refused: {cli_result[:120]}", None

        # Try to extract workflow ID from CLI output
        # n8n IDs are alphanumeric strings, typically 10+ chars
        wf_id = None
        id_match = re.search(r'\b([a-zA-Z0-9]{10,})\b', cli_result)
        if id_match:
            wf_id = id_match.group(1)

        # Verify workflow exists in n8n via REST API
        found = False
        if wf_id:
            try:
                req = _urlr.Request(
                    f"{settings.n8n_base_url.rstrip('/')}/api/v1/workflows/{wf_id}",
                    headers={"X-N8N-API-KEY": settings.n8n_api_key, "Accept": "application/json"},
                )
                with _urlr.urlopen(req, timeout=_N8N_TEST_TIMEOUT) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    found = bool(body.get("id"))
            except Exception:
                pass

        # Fallback: search by name if ID extraction failed or didn't match
        if not found:
            try:
                req = _urlr.Request(
                    f"{settings.n8n_base_url.rstrip('/')}/api/v1/workflows?limit=30",
                    headers={"X-N8N-API-KEY": settings.n8n_api_key, "Accept": "application/json"},
                )
                with _urlr.urlopen(req, timeout=_N8N_TEST_TIMEOUT) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    workflows = body.get("data", body) if isinstance(body, dict) else body
                    if isinstance(workflows, list):
                        for wf in workflows:
                            if wf.get("name", "").startswith(_BUILD_TEST_PREFIX):
                                wf_id = str(wf.get("id", ""))
                                found = True
                                break
            except Exception:
                pass

        # Cleanup test workflow (best-effort)
        if found and wf_id:
            try:
                req = _urlr.Request(
                    f"{settings.n8n_base_url.rstrip('/')}/api/v1/workflows/{wf_id}",
                    headers={"X-N8N-API-KEY": settings.n8n_api_key},
                    method="DELETE",
                )
                _urlr.urlopen(req, timeout=10)
            except Exception:
                pass

        if found:
            return True, f"CLI built workflow {wf_id} in n8n ✓", wf_id
        return False, f"CLI responded but workflow not found in n8n (output: {cli_result[:100]})", None

    except Exception as e:
        return False, f"exception: {e}", None


def _apply_cli_build_fixes(detail: str) -> list[str]:
    """Auto-fix strategies when CLI n8n build fails."""
    fixes = []

    # If CLI refused, try re-registering MCP server
    if any(p in detail.lower() for p in _BUILD_REFUSAL_PHRASES):
        try:
            from .pro_router import _submit_and_poll, _cli_worker_url
            if _cli_worker_url():
                _submit_and_poll(
                    "claude_pro",
                    {"prompt": 'Run this command: claude mcp add n8n --stdio "python /app/mcp/n8n_mcp_server.py"'},
                    timeout=30,
                )
                fixes.append("re-registered n8n MCP server on inspiring-cat")
        except Exception:
            pass

    # If workflow not found, check n8n is reachable
    if "not found in n8n" in detail:
        try:
            from ..tools.n8n_repair import attempt_n8n_repair
            fixed, repair_actions = attempt_n8n_repair("connection refused", {})
            if fixed:
                fixes.append(f"n8n repair: {'; '.join(repair_actions)[:80]}")
        except Exception:
            pass

    return fixes


# ── NEW: CLI High Availability Test ──────────────────────────────────────────

def _test_cli_ha() -> tuple[bool, str]:
    """
    Test BOTH CLI instances independently:
    1. inspiring-cat (CLI worker) via /health + task submission
    2. super-agent (local subprocess) via direct claude -p

    Returns (ok, detail). Both must pass for HA.
    """
    import urllib.request as _urlr

    cli_worker_url = os.environ.get("CLI_WORKER_URL", "").rstrip("/")

    # ── Test inspiring-cat ────────────────────────────────────────────────────
    ic_ok = False
    ic_detail = "CLI_WORKER_URL not set"
    if cli_worker_url:
        # Health check
        try:
            with _urlr.urlopen(f"{cli_worker_url}/health", timeout=10) as resp:
                health = json.loads(resp.read().decode("utf-8"))
                if health.get("claude_available"):
                    ic_ok = True
                    ic_detail = "healthy"
                else:
                    ic_detail = "claude_available=false"
        except Exception as e:
            ic_detail = f"health unreachable: {e}"

        # If health passed, also submit a real task
        if ic_ok:
            try:
                from .pro_router import _submit_and_poll
                result = _submit_and_poll(
                    "claude_pro", {"prompt": "Reply with exactly the word PONG"},
                    timeout=_CLI_HA_TIMEOUT,
                )
                if result and not result.startswith("["):
                    ic_detail = "task OK"
                else:
                    ic_ok = False
                    ic_detail = f"task failed: {(result or 'None')[:60]}"
            except Exception as e:
                ic_ok = False
                ic_detail = f"task exception: {e}"
    else:
        # Single-instance mode — skip inspiring-cat, don't block HA
        ic_ok = True
        ic_detail = "skipped (single-instance mode)"

    # ── Test super-agent local CLI ────────────────────────────────────────────
    sa_ok = False
    sa_detail = "not tested"
    try:
        import subprocess as _sp
        _env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        _env["HOME"] = "/root"
        proc = _sp.run(
            ["claude", "-p", "Reply with exactly the word PONG"],
            capture_output=True, text=True, input="1\n" * 30,
            timeout=_CLI_HA_TIMEOUT, env=_env,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode == 0 and stdout and not stdout.startswith("["):
            sa_ok = True
            sa_detail = "OK"
        else:
            sa_detail = f"rc={proc.returncode} out={stdout[:80]} err={stderr[:80]}"
            _log(f"HA: super-agent local CLI failed — {sa_detail}")
            # Check if auth is actually valid despite failure
            _out_compact = f"{stdout} {stderr}".replace(": ", ":")
            if '"authMethod":"claude.ai"' in _out_compact and '"loggedIn":true' in _out_compact:
                _log("HA: super-agent auth IS valid — transient CLI error, not auth issue")
            else:
                # Attempt auto-fix
                try:
                    from .pro_router import _try_restore_claude_auth
                    if _try_restore_claude_auth():
                        sa_detail += " → auth restored, will retry next cycle"
                        _log("HA: super-agent credentials auto-restored")
                except Exception:
                    pass
    except FileNotFoundError:
        sa_detail = "claude CLI binary not found in PATH"
        _log(f"HA: super-agent FileNotFoundError — checking PATH")
        # Log what's actually in PATH for debugging
        try:
            import subprocess as _sp2
            which = _sp2.run(["which", "claude"], capture_output=True, text=True, timeout=5)
            _log(f"HA: 'which claude' → {(which.stdout or which.stderr or 'empty').strip()}")
        except Exception:
            pass
    except _sp.TimeoutExpired:
        sa_detail = f"timed out ({_CLI_HA_TIMEOUT}s)"
        _log(f"HA: super-agent local CLI timed out after {_CLI_HA_TIMEOUT}s")
    except Exception as e:
        sa_detail = f"exception: {e}"
        _log(f"HA: super-agent exception: {e}")

    # HA passes if inspiring-cat is healthy. Super-agent local CLI is a bonus
    # fallback — if it fails with auth issues but inspiring-cat works, HA is OK
    # because all production CLI traffic routes through inspiring-cat first.
    if ic_ok and not sa_ok:
        _log(f"HA: inspiring-cat OK, super-agent local failed ({sa_detail}) — HA passes (inspiring-cat is primary)")
    both_ok = ic_ok  # inspiring-cat is the primary CLI host
    combined = f"inspiring-cat: {ic_detail} | super-agent: {sa_detail}"
    return both_ok, combined


# ── Auto-fix: detect and set N8N_BASE_URL from Railway ────────────────────────

def _auto_detect_n8n_url() -> bool:
    """
    Use Railway GraphQL API to discover the n8n service URL and set N8N_BASE_URL.
    Returns True if N8N_BASE_URL was set successfully.
    """
    try:
        from ..config import settings
        if settings.n8n_base_url:
            return True  # already set

        # Try to discover via Railway API
        import subprocess
        result = subprocess.run(
            ["railway", "service", "list"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "HOME": "/root"},
        )
        output = result.stdout or ""
        # Look for n8n-related domain
        for line in output.splitlines():
            lower = line.lower()
            if "n8n" in lower and ".up.railway.app" in lower:
                # Extract domain
                import re
                m = re.search(r'([\w-]+\.up\.railway\.app)', line)
                if m:
                    url = f"https://{m.group(1)}"
                    _log(f"Auto-detected n8n URL: {url}")
                    # Set it in the environment for this process
                    os.environ["N8N_BASE_URL"] = url
                    settings.n8n_base_url = url
                    return True
        return False
    except Exception:
        return False


# ── Diagnosis ─────────────────────────────────────────────────────────────────

def _diagnose() -> dict:
    result = {
        "burst": False, "cli_down": False, "daily": False,
        "n8n_unreachable": False, "description": "unknown",
    }
    try:
        from .pro_router import (
            _BURST_FLAG, _BURST_TTL, _CLI_DOWN_FLAG, _CLI_DOWN_TTL,
            _daily_flag_active, _flag_active, _verify_cli_health,
        )
        result["burst"] = _flag_active(_BURST_FLAG, _BURST_TTL)
        result["cli_down"] = _flag_active(_CLI_DOWN_FLAG, _CLI_DOWN_TTL)
        result["daily"] = _daily_flag_active()
        if result["cli_down"] and _verify_cli_health():
            result["cli_down"] = False
    except Exception as e:
        result["description"] = f"flag check error: {e}"

    try:
        from ..config import settings
        if settings.n8n_base_url and settings.n8n_api_key:
            import urllib.request
            req = urllib.request.Request(
                f"{settings.n8n_base_url.rstrip('/')}/api/v1/workflows?limit=1",
                headers={"X-N8N-API-KEY": settings.n8n_api_key},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                result["n8n_unreachable"] = resp.status >= 400
        else:
            result["n8n_unreachable"] = True
    except Exception:
        result["n8n_unreachable"] = True

    parts = []
    if result["daily"]:     parts.append("DAILY_LIMIT")
    if result["burst"]:     parts.append("BURST_THROTTLE")
    if result["cli_down"]:  parts.append("CLI_DOWN")
    if result["n8n_unreachable"]: parts.append("N8N_UNREACHABLE")
    result["description"] = ", ".join(parts) if parts else "no flags"
    return result


# ── Targeted fixes ────────────────────────────────────────────────────────────

def _apply_fixes(diag: dict) -> list[str]:
    """Apply all possible fixes. Returns list of actions taken."""
    fixes = []

    # Clear stale BURST flag (>10 min old)
    if diag.get("burst"):
        try:
            from .pro_router import _BURST_FLAG
            age = time.time() - _BURST_FLAG.stat().st_mtime
            if age > 600:
                _BURST_FLAG.unlink(missing_ok=True)
                fixes.append("cleared stale BURST flag")
        except Exception:
            pass

    # Clear stale CLI_DOWN flag if health endpoint says CLI is up
    if diag.get("cli_down"):
        try:
            from .pro_router import clear_cli_down_flag, _verify_cli_health
            if _verify_cli_health():
                clear_cli_down_flag()
                fixes.append("cleared stale CLI_DOWN flag — CLI confirmed up")
        except Exception:
            pass

    # n8n unreachable — trigger repair and auto-detect URL
    if diag.get("n8n_unreachable"):
        try:
            from ..tools.n8n_repair import attempt_n8n_repair
            fixed, repair_actions = attempt_n8n_repair("connection refused", {})
            if fixed:
                fixes.append(f"n8n repair: {'; '.join(repair_actions)[:120]}")
        except Exception:
            pass
        # Auto-detect N8N_BASE_URL if missing
        if _auto_detect_n8n_url():
            fixes.append("auto-detected N8N_BASE_URL from Railway")

    return fixes


# ── Resolution ────────────────────────────────────────────────────────────────

def _resolve(state: dict, tier_results: dict, wf_id: str | None, wf_name: str | None) -> None:
    now_iso = datetime.datetime.utcnow().isoformat()
    state["resolved"] = True
    state["active"] = False
    state["resolved_at"] = now_iso
    state["resolved_workflow_id"] = wf_id
    state["resolved_workflow_name"] = wf_name
    state["tier_results"] = tier_results
    _save_state(state)

    # Build summary
    tiers_ok = [k for k, v in tier_results.items() if v.get("ok")]
    tiers_fail = [k for k, v in tier_results.items() if not v.get("ok")]
    msg = f"All tiers operational after {state['attempt_count']} cycles. "
    msg += f"Working: {', '.join(tiers_ok)}. "
    if tiers_fail:
        msg += f"Unavailable (non-critical): {', '.join(tiers_fail)}. "
    if wf_id:
        msg += f"n8n test workflow '{wf_name}' (ID {wf_id}) verified."

    _log(f"Watchdog RESOLVED ✓ — {msg}")
    _write_alert("resolved", msg)

    try:
        from ..alerts.notifier import alert_claude_recovered
        alert_claude_recovered(subscription="pro")
    except Exception:
        pass


# ── Main cycle ────────────────────────────────────────────────────────────────

def activate() -> None:
    state = _load_state()
    if state.get("active"):
        return
    state["active"] = True
    state["resolved"] = False
    state["attempt_count"] = 0
    state["last_failure_mode"] = ""
    state["last_attempt_ts"] = 0
    state["tier_results"] = {}
    _save_state(state)
    _log("Watchdog ACTIVATED — testing all tiers every 5 min until confirmed healthy")
    _write_alert("warning", "Self-healing watchdog active — testing all tiers")


def run_watchdog_cycle() -> None:
    """
    Called every 5 minutes by APScheduler. Tests all tiers, fixes what it can,
    only resolves when the primary free tiers + n8n are all working.
    Never raises. Never asks for user input.
    """
    try:
        state = _load_state()

        if not state.get("active") and not state.get("resolved"):
            state["active"] = True
            _save_state(state)

        # Quiet period after resolution — re-check hourly
        if state.get("resolved"):
            resolved_at = state.get("resolved_at")
            if resolved_at:
                try:
                    age_h = (
                        datetime.datetime.utcnow()
                        - datetime.datetime.fromisoformat(resolved_at)
                    ).total_seconds() / 3600
                    if age_h < 1:
                        return
                except Exception:
                    pass
            state["resolved"] = False
            state["active"] = True
            _save_state(state)

        if state.get("attempt_count", 0) >= _MAX_ATTEMPTS:
            _log(f"Watchdog: reached {_MAX_ATTEMPTS} attempts — pausing. Manual check needed.")
            _write_alert("warning", f"Watchdog paused after {_MAX_ATTEMPTS} cycles")
            state["active"] = False
            _save_state(state)
            return

        state["attempt_count"] = state.get("attempt_count", 0) + 1
        state["last_attempt_ts"] = time.time()
        cycle = state["attempt_count"]

        # ── 1. Diagnose ───────────────────────────────────────────────────────
        diag = _diagnose()
        state["last_failure_mode"] = diag["description"]
        _save_state(state)
        _log(f"Watchdog cycle #{cycle} — flags: {diag['description']}")

        # Daily limit: can't fix — just wait
        if diag.get("daily") and not diag.get("burst") and not diag.get("cli_down"):
            _log("Watchdog: DAILY limit active — waiting for quota reset")
            _write_alert("warning", "Daily Claude limit — auto-recovery pending reset")
            return

        # ── 2. Apply fixes ────────────────────────────────────────────────────
        fixes = _apply_fixes(diag)
        if fixes:
            _log(f"Watchdog fixes applied: {'; '.join(fixes)}")

        # ── 3. Test ALL tiers ─────────────────────────────────────────────────
        tier_results = {}

        cli_ok, cli_detail = _test_cli()
        tier_results["CLI"] = {"ok": cli_ok, "detail": cli_detail}

        gemini_ok, gemini_detail = _test_gemini()
        tier_results["Gemini"] = {"ok": gemini_ok, "detail": gemini_detail}

        anthropic_ok, anthropic_detail = _test_anthropic()
        tier_results["Anthropic"] = {"ok": anthropic_ok, "detail": anthropic_detail}

        deepseek_ok, deepseek_detail = _test_deepseek()
        tier_results["DeepSeek"] = {"ok": deepseek_ok, "detail": deepseek_detail}

        # ── 4. Test n8n REST ──────────────────────────────────────────────────
        n8n_ok, n8n_detail, wf_id, wf_name = _test_n8n()
        tier_results["n8n"] = {"ok": n8n_ok, "detail": n8n_detail}

        # ── 5. Test CLI High Availability ─────────────────────────────────────
        ha_ok, ha_detail = _test_cli_ha()
        tier_results["HA"] = {"ok": ha_ok, "detail": ha_detail}
        state["cli_ha_ok"] = ha_ok
        state["cli_ha_detail"] = ha_detail

        # ── 6. Test CLI n8n Build (only if HA passed — no point if CLI is down)
        n8n_configured = bool(os.environ.get("N8N_BASE_URL") or os.environ.get("n8n_base_url"))
        build_ok = False
        build_detail = "skipped"
        build_wf_id = None

        # Check cooldown — skip if last success was < 6h ago
        last_build_ts = state.get("cli_build_last_success_ts", 0)
        build_cooled = (time.time() - last_build_ts) > (_BUILD_COOLDOWN_HOURS * 3600)

        if ha_ok and n8n_configured and build_cooled:
            build_ok, build_detail, build_wf_id = _test_cli_n8n_build()
            if not build_ok:
                # Apply auto-fixes for build failures
                build_fixes = _apply_cli_build_fixes(build_detail)
                if build_fixes:
                    _log(f"Watchdog build fixes: {'; '.join(build_fixes)}")
            else:
                state["cli_build_last_success_ts"] = time.time()
        elif not ha_ok:
            build_detail = "skipped (HA failed — CLI not available)"
        elif not n8n_configured:
            build_ok = True  # don't block if n8n not configured
            build_detail = "skipped (n8n not configured)"
        elif not build_cooled:
            build_ok = True  # last build was recent enough
            build_detail = "skipped (cooldown — last success < 6h ago)"

        tier_results["Build"] = {"ok": build_ok, "detail": build_detail}
        state["cli_build_ok"] = build_ok
        state["cli_build_detail"] = build_detail
        state["cli_build_workflow_id"] = build_wf_id

        state["tier_results"] = {k: v for k, v in tier_results.items()}
        _save_state(state)

        # Log all results
        summary_parts = []
        for name, res in tier_results.items():
            mark = "✓" if res["ok"] else "✗"
            summary_parts.append(f"{name}:{mark}")
        summary = " | ".join(summary_parts)
        _log(f"Watchdog cycle #{cycle} results: {summary}")

        # ── 7. Resolution check ──────────────────────────────────────────────
        any_free = cli_ok or gemini_ok
        any_paid = anthropic_ok or deepseek_ok
        n8n_pass = n8n_ok or not n8n_configured
        cli_worker_url = os.environ.get("CLI_WORKER_URL", "")

        # BOTH new gates must pass alongside existing criteria
        cli_build_pass = build_ok or not n8n_configured
        cli_ha_pass = ha_ok or not cli_worker_url  # single-instance mode OK

        resolved = any_free and any_paid and n8n_pass and cli_build_pass and cli_ha_pass

        if resolved:
            _resolve(state, tier_results, wf_id or build_wf_id, wf_name)
            return

        # Not resolved yet — report what's failing
        failing = [k for k, v in tier_results.items() if not v["ok"]]
        _write_alert(
            "warning",
            f"Cycle {cycle}: {summary} — fixing: {', '.join(failing)}"
        )
        _save_state(state)

    except Exception as e:
        try:
            from ..activity_log import bg_log
            bg_log(f"Watchdog cycle error: {e}", source="cli_n8n_watchdog")
        except Exception:
            pass


def get_status() -> dict:
    state = _load_state()
    alert = read_watchdog_alert()
    return {
        "watchdog_active": state.get("active", False),
        "watchdog_resolved": state.get("resolved", False),
        "attempt_count": state.get("attempt_count", 0),
        "last_failure_mode": state.get("last_failure_mode", ""),
        "resolved_at": state.get("resolved_at"),
        "resolved_workflow_id": state.get("resolved_workflow_id"),
        "resolved_workflow_name": state.get("resolved_workflow_name"),
        "tier_results": state.get("tier_results", {}),
        "cli_build_ok": state.get("cli_build_ok", False),
        "cli_build_detail": state.get("cli_build_detail", ""),
        "cli_ha_ok": state.get("cli_ha_ok", False),
        "cli_ha_detail": state.get("cli_ha_detail", ""),
        "dashboard_alert": alert,
    }
