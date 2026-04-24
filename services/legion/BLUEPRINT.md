# Legion Engineer — Integration Blueprint

This document is the **single source of truth for rebuilding the full
three-service stack** (inspiring-cat + super-agent + legion + shared
Postgres + n8n) on a fresh Railway project, a different VPS, or local
Docker Compose. It records *what* each piece is, *why* it exists, and
*what order* to stand it up in.

No real credential values appear in this file. Every secret is referenced
by env var name only. Populate them from your password manager on rebuild.

---

## 1. System overview

Three independently deployed services, one shared data plane.

```
                 ┌──────────── user / dashboard ────────────┐
                 │                                           │
                 ▼                                           ▼
        ┌─────────────────┐                          ┌─────────────────┐
        │   super-agent   │   response priority      │       n8n       │
        │ (API/router/UI) │ ──────────────────────►  │ magic-link +    │
        └───────┬─────────┘                          │ trash-purge +   │
                │                                    │ business flows  │
      ┌─────────┴──────────┐                         └────────┬────────┘
      ▼                    ▼                                  │
┌─────────────┐      ┌─────────────┐                          │
│ inspiring-  │      │   legion    │                          │
│ cat (1°)    │      │ hive (2°)   │ ◄─── magic links ────────┘
│ Claude CLI  │      │ multi-agent │
│ Account A   │      │ Account B   │
└──────┬──────┘      └──────┬──────┘
       │ last resort        │
       ▼                    ▼
 Anthropic API       Anthropic API
 Haiku → Sonnet      Haiku → Sonnet
 → Opus → DeepSeek   → Opus → DeepSeek
       │
       └──── all services read/write ────┐
                                          ▼
                                  ┌──────────────┐
                                  │  Postgres    │
                                  │ (shared)     │
                                  └──────────────┘
```

**Response priority** (strict):
1. `inspiring-cat` Claude CLI (primary)
2. `legion` hive (secondary, this service)
3. Anthropic API Haiku → Sonnet → Opus → DeepSeek (last resort)

---

## 2. Services inventory

| service | repo location | purpose | deploys from |
|---|---|---|---|
| `super-agent` | GitHub:`gelson12/super-agent` (root) | API gateway, request routing, dashboard UI, n8n tool definitions | repo root `Dockerfile` |
| `inspiring-cat` | same repo, separate Railway service | Claude CLI (Account A) + Gemini CLI host with 4-layer session healing | repo root, different start cmd |
| `legion` (this) | same repo, `services/legion/` subdir | Hive fallback container, Claude CLI Account B, Kimi, Ollama, HF, Gemini-B | Railway Root Directory = `services/legion` |
| `n8n` | Railway template | Workflow engine: magic-link extraction, trash purge, business automations | Railway template |
| `Postgres` | Railway managed PG | Shared data plane for all services | Railway PG plugin |

**Isolation model:** Legion shares the super-agent GitHub repo but is a **completely independent Railway service** with its own Dockerfile, own container, own crash domain, own env vars. The repo is shared for monorepo convenience (one PR surface, shared tooling); runtime isolation is enforced by Railway treating each service's `rootDirectory` independently. Legion code must **not** import from super-agent's Python modules — the boundary is convention-enforced, not physical.

---

## 3. External dependencies (what to provision before rebuild)

| dependency | what you need | where |
|---|---|---|
| GitHub account | One repo (`super-agent`, private) — Legion lives in `services/legion/` | github.com |
| Railway account | One project, five services | railway.app |
| Claude Account A | Email `gelsonmascarenhas@gmail.com` (or your A), Pro subscription | claude.ai |
| Claude Account B | Email `e.remote.demands@gmail.com` (or your B), Pro subscription | claude.ai |
| Hotmail inbox | `gelson_m@hotmail.com` — magic-link delivery target for both A and B (Gmail forwards to here) | outlook.live.com |
| Microsoft Outlook OAuth | App registration with IMAP/Graph read + delete scopes | portal.azure.com |
| Anthropic API key | For last-resort fallback + suitability classifier | console.anthropic.com |
| Kimi account (optional) | Email + API key | moonshot.ai / kimi.com |
| Gemini API key × 2 | One per Gemini-CLI account | aistudio.google.com |
| Hugging Face API key | `HF_API_KEY` — inference endpoint access | huggingface.co |
| DeepSeek API key (optional) | Last-resort cascade only | platform.deepseek.com |

---

## 4. Env var inventory — by service

Populate from password manager. Every var is referenced by **name only** below.

### 4.1 Shared (all services that touch PG / n8n)
| var | notes |
|---|---|
| `PG_DSN` | single shared PG, all services |
| `N8N_BASE_URL` | n8n public Railway URL |
| `N8N_API_KEY` | n8n-issued API key (rotate if leaked) |

### 4.2 super-agent
| var | purpose |
|---|---|
| `ANTHROPIC_API_KEY` | last-resort cascade |
| `GEMINI_API_KEY` | Gemini API fallback |
| `DEEPSEEK_API_KEY` | deepest last-resort tier |
| `INSPIRING_CAT_URL` | base URL of inspiring-cat for health probes |
| `LEGION_BASE_URL` | Legion's public URL (P5 dispatcher hook) |
| `LEGION_API_SHARED_SECRET` | HMAC with Legion — same value in both services |
| `GITHUB_PAT` | github_agent tool |
| `MEMORY_INGEST_SECRET` | unified memory system |

### 4.3 inspiring-cat
| var | purpose |
|---|---|
| `CLAUDE_SESSION_TOKEN` | L2 env-var credentials backup (base64 JSON) |
| `ANTHROPIC_EMAIL` | Account A email for healing |
| `WEBHOOK_SECRET` | HMAC on `/webhook/verification-code` for A |
| `N8N_MAGIC_LINK_WEBHOOK` | callback URL shared with n8n |

### 4.4 legion (this service)
| var | purpose |
|---|---|
| `PG_DSN` | shared |
| `LEGION_API_SHARED_SECRET` | must match super-agent's value |
| `LEGION_WEBHOOK_SECRET` | HMAC on Legion's `/webhook/verification-code` (P3) |
| `PRIMARY_BEACON_SECRET` | HMAC on primary-healthy beacons from inspiring-cat |
| `PRIMARY_HEALTH_URL` | `http://inspiring-cat:8003/auth/login-status` (or public URL) |
| `CLAUDE_ACCOUNT_B_EMAIL` | Account B email |
| `CLAUDE_ACCOUNT_B_SESSION_TOKEN` | Account B credentials backup |
| `CLAUDE_ACCOUNT_B_REFRESH_TOKEN` | if OAuth L3 ever fixed |
| `KIMI_ACCOUNT_EMAIL`, `KIMI_API_KEY` | Kimi agent |
| `OLLAMA_API_KEY` | optional remote Ollama |
| `GEMINI_ACCOUNT_B_EMAIL`, `GEMINI_API_KEY_B` | Gemini-B agent |
| `HF_API_KEY`, `HF_COST_CAP_USD` | HF smart discovery |
| `ANTHROPIC_API_KEY_HAIKU_CLASSIFIER` | suitability rubric |
| `N8N_MAGIC_LINK_WEBHOOK` | shared callback URL |
| feature flags | `LEGION_ENABLED`, `L5_ENABLED`, `HF_ENABLED`, `OLLAMA_ENABLED`, `KIMI_ENABLED`, `DUAL_ACCOUNT_ENABLED`, `HIVE_EARLY_TERMINATION` |

### 4.5 n8n
| var | purpose |
|---|---|
| `INSPIRING_CAT_WEBHOOK_URL` | `https://inspiring-cat-...railway.app/webhook/verification-code` |
| `LEGION_WEBHOOK_URL` | Legion's magic-link endpoint (set only after P3 ships `/webhook/verification-code`) |
| `WEBHOOK_SECRET` | must match inspiring-cat's value |
| `LEGION_WEBHOOK_SECRET` | must match Legion's value |
| `N8N_SELF_API_KEY` | for self-calling workflows |
| Outlook OAuth credential | stored *inside* n8n's credential store, not env |

---

## 5. Secrets that must be generated (vs. obtained from providers)

| secret | generation |
|---|---|
| `LEGION_API_SHARED_SECRET` | 64 hex chars: `-join ((48..57) + (97..122) \| Get-Random -Count 64 \| %{[char]$_})` |
| `LEGION_WEBHOOK_SECRET` | same recipe |
| `PRIMARY_BEACON_SECRET` | same recipe |
| `WEBHOOK_SECRET` (inspiring-cat) | same recipe |
| `MEMORY_INGEST_SECRET` | same recipe |

Every HMAC-shared secret must be set to the **identical** value in the two services that share it.

---

## 6. Postgres schema

Two repos contribute additive schemas to the single shared PG. Apply in order.

| file | source repo | contents |
|---|---|---|
| super-agent's existing migrations | `super-agent/migrations/` | agent_activity, agent_interactions, self_improve_*, intelligence_*, unified_memory, bridge.* schema |
| `services/legion/migrations/0001_legion_base.sql` | this repo | `claude_account_state`, `hive_rounds`, `hive_agent_scores`, `agent_quota` |

All schemas are `CREATE IF NOT EXISTS` and safe to re-run.

**Shared write contracts:**
- `claude_account_state` — written by *both* inspiring-cat (row `A`) and legion (row `B`). Coordination uses `pg_try_advisory_lock(hashtext('claude_active'))` — only the lock-holder sets `role='active'`. PG is source of truth; beacons are a latency optimization.

---

## 7. n8n workflows

| workflow JSON | id in prod | purpose |
|---|---|---|
| `super-agent/n8n/claude_verification_monitor.json` | `jxnZZwTqJ7naPKc6` | Poll Hotmail every 30s, extract magic links, route by account A/B to the matching container |
| `super-agent/n8n/claude_trash_purge.json` | (new, auto-assigned) | Every 6h, hard-delete messages older than 15 min from Deleted Items / Trash / Junk |
| `super-agent/n8n/secretary_workflow.json` | existing | General Outlook ops |
| `super-agent/n8n/finance_operations_controller.json` | existing | Finance email monitor |

Import via `super-agent/scripts/import_legion_n8n.ps1`. Outlook OAuth credential must already exist inside n8n (credential id `9paQ8IJLgiLbjEYI` at time of writing).

---

## 8. Bootstrap order — fresh build

1. **Provision accounts and API keys** from section 3.
2. **Create Railway project** with Postgres plugin.
3. **Create n8n service** from Railway template. Capture `N8N_BASE_URL` and generate `N8N_API_KEY` inside n8n. Set `N8N_SELF_API_KEY` to the same value.
4. **Configure Outlook OAuth in n8n UI** → Credentials → add Microsoft Outlook OAuth2. Record the credential id; you'll need it if you ever re-import workflows that reference it.
5. **Push `super-agent` repo** to GitHub. Create Railway service from it. Create a second Railway service from the same repo with a different start command for `inspiring-cat`. Set all env vars from §4.2 and §4.3.
6. **Apply super-agent migrations**: `psql "$PG_DSN" -f super-agent/migrations/*.sql` in filename order.
7. **Create Legion Railway service** pointing at the same `super-agent` repo, with **Root Directory = `services/legion`**. Set env vars from §4.4 (all feature flags stay `false` on first deploy).
8. **Apply Legion migration**: `services/legion/scripts/apply_migrations.ps1` with `PG_DSN` set, OR paste `services/legion/migrations/0001_legion_base.sql` into Railway's PG query tab.
9. **Import n8n workflows**: `super-agent/scripts/import_legion_n8n.ps1` with `N8N_BASE_URL` + `N8N_API_KEY` set.
10. **Set n8n service vars** from §4.5.
11. **Smoke test**: `curl https://<legion-host>/health` → expect `legion_enabled: false`. Dashboard should render all three widgets.
12. **Flip feature flags progressively** once per-phase verification passes: KIMI_ENABLED → OLLAMA_ENABLED + HF_ENABLED → LEGION_ENABLED → DUAL_ACCOUNT_ENABLED → L5_ENABLED.

---

## 9. Rebuild on a different host (VPS / Docker Compose)

Railway-specific pieces and their portable equivalents:

| Railway | VPS / Docker Compose equivalent |
|---|---|
| Service env vars | `.env` file per container (**never commit**) |
| Managed Postgres | Postgres 16 container + volume |
| Private networking (`inspiring-cat:8003`) | Docker Compose network; service names resolve as hostnames |
| Public domain | Nginx/Caddy reverse proxy with Let's Encrypt |
| Deploy from GitHub | `git pull && docker compose build && docker compose up -d` |
| Railway Postgres query tab | `psql "$PG_DSN"` from the host |
| Build from Dockerfile | identical — every service has its own `Dockerfile` / `Dockerfile.legion` |
| `railway.toml` healthcheck | Compose `healthcheck:` blocks |

A minimal Compose skeleton (not committed — write at rebuild time):

```yaml
services:
  postgres:
    image: postgres:16
    environment: { POSTGRES_DB: legion, POSTGRES_PASSWORD: ... }
    volumes: [pgdata:/var/lib/postgresql/data]

  n8n:
    image: n8nio/n8n
    environment: { N8N_HOST: ..., N8N_API_KEY: ..., ... }
    volumes: [n8n_data:/home/node/.n8n]

  super-agent:
    build: ./super-agent
    env_file: .env.super-agent
    depends_on: [postgres, n8n]

  inspiring-cat:
    build: ./super-agent
    command: ["/app/entrypoint.cli.sh"]
    env_file: .env.inspiring-cat
    depends_on: [postgres, n8n]

  legion:
    build: ./super-agent/services/legion
    env_file: .env.legion
    depends_on: [postgres, n8n]

volumes:
  pgdata:
  n8n_data:
```

Reverse proxy terminates TLS and routes:
- `super-agent.example.com` → `super-agent:8080`
- `inspiring-cat.example.com` → `inspiring-cat:8080`
- `legion.example.com` → `legion:8080`
- `n8n.example.com` → `n8n:5678`

---

## 10. Healing chain summary

| layer | file | purpose | container |
|---|---|---|---|
| L1 volume backup | `cli_auto_login.py` | restore from `/workspace/.claude_credentials_backup.json` | inspiring-cat (A), legion (B) |
| L2 env var | `cli_auto_login.py` | restore from `CLAUDE_SESSION_TOKEN` base64 | both |
| L3 OAuth refresh | `cli_auto_login.py` | POST refresh_token to `claude.com/cai/oauth/token` (currently HTTP 405, broken) | both |
| L4 Playwright + n8n magic-link | `cli_auto_login.py` + `n8n jxnZZwTqJ7naPKc6` | headless browser login, n8n reads magic link from Hotmail | both |
| L5 DevBrowser-CDP (planned P4) | `app/healing/l5_devbrowser.py` | code-first CDP recovery when L4 fails | both |

Mailbox hygiene (**important**): the n8n verification workflow deletes consumed emails; `claude_trash_purge.json` hard-deletes from trash every 6h. Never let the shared inbox fill up or all healing breaks silently.

---

## 11. Known failure modes + recovery (quick reference)

Full table in `~/.claude/plans/design-and-implement-a-jaunty-wave.md` §11. Top five:

1. **Account A token expired** → inspiring-cat L1→L5 → if all fail, flip A→EXHAUSTED and Legion takes over on Account B.
2. **Both accounts EXHAUSTED** → last-resort Anthropic API cascade → pager alert.
3. **Split-brain (both ACTIVE)** → PG advisory lock prevents; passive side self-demotes on read.
4. **HF daily cost cap hit** → disable HF agent for 24h.
5. **n8n webhook down** → skip to L5; alert if L5 also fails.

---

## 12. Phase tracker

| phase | status | git tag / commit |
|---|---|---|
| P0 scaffolding | complete | super-agent `34d8097` (imported via subtree) |
| P1 single-agent (Kimi) | complete | super-agent `0ac87a2` (imported via subtree) |
| P1 Track B n8n patch | complete | super-agent `ba80a2b` + `61e8497` |
| Monorepo consolidation | complete | super-agent `985d3e8` (subtree merge) |
| Railway deploy hardening | complete | super-agent `4eac54c` + `a2932ec` + `94281fc` + `2a277bb` + `005e2a2` + `82839c3` — PATH, Kimi/uv deferral, railway.toml removal, zstd for Ollama, supervisord autostart=false for feature-gated programs |
| **Legion live 2026-04-24** | — | https://legion-production-36db.up.railway.app/health returns 200 (deployment 948f7836) |
| P2 hive fan-out | complete | super-agent `6e8d3a7` — rank engine, config loader, suitability classifier, hive orchestrator, Gemini-B + Ollama + HF adapters, curated.yaml, tests. Stays dormant until LEGION_ENABLED flips. |
| P3 dual-Claude + ChatGPT | complete | super-agent `530a7e7` — state machine (`claude_account_state` row-lock coordination), `claude_b` CLI agent (HOME-scoped), healing chain L1/L2 fully wired + L3/L4 stubs, `claude_b_watchdog` daemon (DUAL_ACCOUNT_ENABLED gated), HMAC primary-beacon endpoint, failover guard in hive engine, bonus ChatGPT agent via OpenAI Chat Completions (registered under CHATGPT_ACCOUNT_EMAIL label). 6 agents now registered. |
| P4 L4 real + L5 DevBrowser-CDP | complete | super-agent `a6e78c9` — real Playwright magic-link flow for Account B with failure-signature classification (TIMEOUT / SELECTOR_NOT_FOUND / NETWORK / CAPTCHA / LOCKED / INVALID_CREDENTIALS / LAUNCHER_FAILED); L5 spawns chromium subprocess with `--remote-debugging-port=9222` and connects via `connect_over_cdp` to sidestep launcher bugs; diagnostics writer scrubs secrets via `app.redact` before writing `trace.json`/`dom.html`/`console.log`/`screenshot.png`/`network.har` to `/workspace/legion/diag/<ts>_<acct>/`; new `POST /webhook/verification-code` HMAC-verified endpoint rendezvous via module-level asyncio queue; chain skips L5 on terminal LOCKED/INVALID_CREDENTIALS and flips account to LOCKED. All gated behind L5_ENABLED=false + DUAL_ACCOUNT_ENABLED=false. Live smoke post-deploy: `/webhook/verification-code` returns 401 with missing secret (vs 404 in P3) — confirms P4 is serving. |
| P2 hive fan-out | pending | — |
| P3 dual-Claude | pending | — |
| P4 L5 DevBrowser-CDP | pending | — |
| P5 widget | pending | — |
| P6 hardening | pending | — |

Keep this table updated at the end of every phase commit.

---

## 13. Pointers

- Architecture plan: `~/.claude/plans/design-and-implement-a-jaunty-wave.md`
- Claude memory (persistent): `~/.claude/projects/<project>/memory/MEMORY.md`
- Relevant memory entries:
  - `feedback_legion_isolation.md` — this repo must stay independent
  - `feedback_mailbox_hygiene.md` — never let Hotmail inbox fill up
  - `feedback_cli_container_ownership.md` — no CLI/PTY code in super-agent
  - `project_claude_cli_healing.md` — 4-layer healing design
  - `project_cli_auth_flow.md` — current auth flow health
