# Legion Engineer

Fallback orchestration container. Sits between `inspiring-cat` (primary Claude CLI)
and the paid Anthropic API / DeepSeek last-resort tier.

Hosts a competitive hive of agents (Kimi, Ollama, Claude-B, Gemini-B, Hugging Face)
that fan out in parallel and return the best-ranked answer. Also owns Claude
Account B's session lifecycle so the two Claude accounts can fail over to each
other without going cold.

**Isolation rule:** this repo is standalone. It does not share code, deployment,
or Dockerfile with `super-agent` or `inspiring-cat`. Coordination is via HTTP
(HMAC-signed) and shared Postgres tables only.

## Response priority

1. `inspiring-cat` Claude CLI (primary, unchanged)
2. **Legion hive** (this service)
3. Anthropic API Haiku → Sonnet → Opus → DeepSeek (last-resort, unchanged)

## Status

- **P0 scaffolding** — in progress. See `~/.claude/plans/design-and-implement-a-jaunty-wave.md`.

## Layout

```
Dockerfile              Python 3.12 + Node 20 + Playwright + Ollama + CLIs
supervisord.conf        nginx, legion-api, ollama, claude-b-watchdog, kimi-keeper, hive-metrics
nginx.conf              8080 public → 8010 FastAPI
railway.toml            Railway build + healthcheck config
app/
  main.py               FastAPI /health + /v1/respond (hive)
  config.py             pydantic-settings env loader
  redact.py             log + diag secret redactor (applied at root logger)
  agents/               kimi, ollama, claude_b, gemini_b, hf
  healing/              L1 volume, L2 env, L3 oauth, L4 playwright, L5 devbrowser-cdp
  hf/                   smart-discovery client + curated.yaml
migrations/
  0001_legion_base.sql  claude_account_state, hive_rounds, hive_agent_scores, agent_quota
```

## Local smoke

```bash
pip install -e .
uvicorn app.main:app --port 8010
curl http://127.0.0.1:8010/health
```

## Deploy

Railway service is standalone. Apply migration once:

```bash
psql "$PG_DSN" -f migrations/0001_legion_base.sql
```

All secrets live in Railway env vars — see `.env.example` for the list of
variable names. Never commit populated `.env`.
