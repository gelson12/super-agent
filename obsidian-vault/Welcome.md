---
type: overview
tags: [system, overview, super-agent]
last_updated: 2026-04-19
---

# Super Agent — Knowledge Vault

This vault is the persistent memory and knowledge base for Super Agent. Every agent reads from and writes to it.

## System Architecture

- **API service** (`app/main.py` port 8001) — FastAPI, handles all `/chat`, `/memory`, `/dashboard` routes
- **CLI Worker** (`cli_worker/main.py` port 8002) — durable task queue for Claude/Gemini CLI subprocesses
- **Obsidian Vault MCP** (this vault, port 22360) — knowledge store, patterns, outcomes, briefings
- **n8n** (port 3000) — workflow automation, webhook triggers, scheduled jobs
- **PostgreSQL** (Railway `divine-contentment`) — session memory, agent_memories (pgvector), insights

## Agents

| Agent | Trigger keywords | Purpose |
|-------|-----------------|---------|
| Shell | build, run, execute, flutter, apk, deploy | Terminal access, builds, Railway ops |
| GitHub | repo, PR, commit, push, html, website | Code read/write, PRs, website |
| N8N | workflow, automation, cron, webhook | n8n workflow management |
| Self-Improve | fix, redeploy, diagnose, self, improve | Autonomous repair and evolution |

## Key File Locations

- Agent patterns: `{Agent}/patterns.md`
- Agent outcomes: `KnowledgeBase/{Agent}/outcomes.md`
- Daily briefings: `Daily/YYYY-MM-DD-briefing.md`
- Engineering reviews: `Engineering/Daily Review YYYY-MM-DD.md`
- Error catalog: `KnowledgeBase/errors.md`

## Learning Systems Active

- **Wisdom store** — cumulative model win-rates, persists to Cloudinary
- **Trajectory predictor** — session sequence learning, predicts next agent
- **Behavior patterns** — time-of-day and agent-transition predictions
- **Adapter** — drift detection, user preference scores, Haiku ceiling calibration
- **Nightly review** — daily health snapshot written here at midnight
- **Vault insight hook** — auto-logs significant agent outcomes after each response

## Owner

Gelson M (gelson_m@hotmail.com) — Bridge Digital Solution
Railway project: divine-contentment | Domain: bridge-digital-solution.com
