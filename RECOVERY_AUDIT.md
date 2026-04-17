# Claude CLI Pro Session Token Recovery — Audit Findings

> **Purpose:** Shared findings between Claude sessions. Each session appends its findings.
> **Status:** 🔴 TOKEN EXPIRED — Recovery in progress

---

## Architecture Overview

```
inspiring-cat (CLI worker container)          super-agent (FastAPI container)
─────────────────────────────────────         ──────────────────────────────
cli_worker/main.py                            app/main.py
  ├── APScheduler watchdog (90s)                ├── /webhook/refresh-cli-token
  ├── pro_cli_watchdog.maybe_recover()          └── /credits/pro-status
  └── full_recovery_chain()
        ├── Layer 1: _try_direct_refresh()
        ├── Layer 3: GitHub Actions relay
        └── Layer 4: Playwright + n8n
```

**Token TTL:** 72h (Anthropic OAuth access_token)
**Proactive window:** 6h before expiry → triggers recovery before failure

---

## Layers

| Layer | Mechanism | Status | Notes |
|-------|-----------|--------|-------|
| 1 | Direct POST to `claude.com/cai/oauth/token` | ⚠️ Always blocked | Cloudflare WAF blocks Railway datacenter IPs. Expected failure. Exists as fast-path attempt. |
| 2 | PostgreSQL `claude_credentials` table | ✅ Implemented | Save: `_push_token_to_railway()`. Restore: boot `lifespan()`. DB always reachable inside Railway. |
| 3 | GitHub Actions relay (`oauth_refresh.yml`) | ⚠️ Needs env vars | Requires `OAUTH_RELAY_SECRET` in inspiring-cat + as GitHub secret. Silently skipped if not set. |
| 4 | Playwright browser + n8n magic link | 🔴 Depends on n8n | Needs `claude_verification_monitor` workflow ACTIVE in n8n. Timeout = 360s. |

---

## Session 1 Findings (2026-04-17)

### Bugs Found and Fixed

**Bug 1 — CRITICAL (cli_worker/main.py:285): Boot recovery lambda never fired**

```python
# BROKEN (short-circuit: __import__("sys") is truthy, or-chain stops there)
asyncio.get_event_loop().run_in_executor(None,
    lambda: __import__("sys") or __import__("sys").path.insert(0, "/app") or
            __import__("app.learning.cli_auto_login", fromlist=["full_recovery_chain"]).full_recovery_chain())

# FIXED (proper function, not lambda)
def _boot_recover():
    import sys; sys.path.insert(0, "/app")
    from app.learning.cli_auto_login import full_recovery_chain as _frc
    _frc()
asyncio.get_event_loop().run_in_executor(None, _boot_recover)
```

**Effect:** When inspiring-cat boots with an expired volume backup, the intended "immediate recovery" silently did nothing. The watchdog (90s delay) was the only fallback.

**Bug 2 — MODERATE (cli_worker/main.py:245): DB restore skipped when backup is expired**

```python
# BROKEN: only restores from DB when backup file is ABSENT
if not _vol_backup.exists():
    _restore_credentials_from_db()
else:
    pass  # "backup present — DB restore not needed" — WRONG if backup is expired

# FIXED: also restore from DB when backup file EXISTS but is expired
_vol_expired = (check expiresAt in backup file)
if not _vol_backup.exists() or _vol_expired:
    _restore_credentials_from_db()
```

**Effect:** If volume backup is present but expired (common after 72h), a fresh DB record was ignored. This blocked Layer 2 from helping on container restart.

### Root Cause of Current TOKEN ERR

1. Token expired (72h OAuth TTL)
2. `_try_restore_claude_auth()` correctly **skips** env var restore (detects expired token via `expiresAt`)
3. `full_recovery_chain()` fires — but Cloudflare blocks Layer 1
4. Layer 4 (Playwright) fires — but requires n8n's `claude_verification_monitor` to deliver magic link
5. **If n8n was down or the workflow was inactive** → 360s timeout → `_last_playwright_timeout` set → 20-min backoff
6. Repeated timeouts may have accumulated in `.playwright_attempts.json`
7. Token expired while recovery was stuck in backoff

### Blocking Files to Check

Run these in the Shell Agent:
```
check /workspace/.claude_login_ratelimit — if exists, shows cooldown (1h/4h/24h)
check /workspace/.playwright_attempts.json — if >3 entries in last 1h, thrashing alert
check /workspace/.recovery_in_progress.lock — if exists, recovery is running or stuck
```

### n8n Workflow Status — MUST CHECK

The `claude_verification_monitor` workflow (ID: `jun8CaMnNhux1iEY`) must be:
- **Active** in n8n
- Monitoring Hotmail inbox (`gelson_m@hotmail.com`)
- POSTing magic link URL to `$SELF_URL/webhook/verification-code`

### Commits From This Session That Did NOT Break Recovery

All commits from 2026-04-17 session 2 (`c7816c1`, `418bf9d`, `b94859c`, `430812b`) only touched:
- `dispatcher.py` — routing improvements
- `agents.html` / `index.html` — Daphney avatar
- New learning files (`trajectory_predictor.py`, etc.)

**None of these touch CLI recovery code.**

---

## How to Run the E2E Test

From the Super Agent chat, type:
```
run python3 /app/scripts/test_recovery_layers.py in the inspiring-cat container
```

Or via Shell Agent:
```
cd /app && python3 scripts/test_recovery_layers.py
```

Results written to `/workspace/RECOVERY_TEST_RESULTS.md`.

---

## Required Railway Variables (inspiring-cat)

| Variable | Required By | Status |
|----------|-------------|--------|
| `CLAUDE_SESSION_TOKEN` | Layer 2 env restore | Set (expired token) |
| `DATABASE_URL` | Layer 2 PostgreSQL | Should be set |
| `N8N_BASE_URL` | Layer 4 n8n check | Should be set |
| `N8N_API_KEY` | Layer 4 n8n check | Should be set |
| `WEBHOOK_SECRET` | Layer 4 security | Optional (skipped if unset) |
| `OAUTH_RELAY_SECRET` | Layer 3 GitHub relay | **Likely MISSING** |
| `SELF_URL` | Layer 3 + Layer 4 | **Likely MISSING** (defaults to hardcoded URL) |
| `SUPER_AGENT_URL` | Token push after recovery | **Likely MISSING** |

---

*Last updated: 2026-04-17 by Session 2*
