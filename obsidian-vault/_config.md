---
type: reference
tags: [config, infrastructure, services, urls]
date: 2026-04-17
---

# Service Configuration Reference

## Railway Services (project: divine-contentment)
| Service | Purpose | Port | Branch |
|---------|---------|------|--------|
| super-agent | FastAPI + Flask main app | 8001/5001 | master |
| inspiring-cat | Claude CLI Pro container + CLI worker | 8765 | master |
| n8n | Workflow automation | 5678 | — |
| obsidian-vault | Obsidian MCP server | 22360 | master |
| PostgreSQL | Shared database | 5432 | — |
| radiant-appreciation | Website (bridge-digital-solution.com) | — | master |

## Internal Railway URLs (container-to-container)
- Obsidian vault MCP: `http://obsidian-vault.railway.internal:22360/sse`
- CLI worker (inspiring-cat): `http://inspiring-cat.railway.internal:8765/run`
- n8n internal: `http://n8n.railway.internal:5678`
- Super-agent: `http://super-agent.railway.internal:8001`

## Environment Variables (names — values in Railway dashboard)
| Variable | Service | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | super-agent | Claude Sonnet + Haiku API |
| `DEEPSEEK_API_KEY` | super-agent | DeepSeek Chat API |
| `GEMINI_API_KEY` | super-agent | Google Gemini (optional) |
| `GITHUB_PAT` | super-agent + inspiring-cat | GitHub Personal Access Token (gelson12) |
| `N8N_API_KEY` | super-agent | n8n REST API key |
| `N8N_BASE_URL` | super-agent | n8n public Railway URL |
| `N8N_WEBHOOK_URL` | n8n | n8n public URL for webhooks |
| `DATABASE_URL` | super-agent | PostgreSQL connection string (include ?sslmode=require) |
| `CLOUDINARY_CLOUD_NAME` | super-agent | Cloudinary cloud name |
| `CLOUDINARY_API_KEY` | super-agent | Cloudinary API key |
| `CLOUDINARY_API_SECRET` | super-agent | Cloudinary API secret |
| `VAULT_PATH` | obsidian-vault | Filesystem path to vault files |
| `SAFE_WORD` | super-agent | Owner authentication safe word |

## GitHub Repos (gelson12)
- `super-agent` — main app, auto-deploys on push to master
- `bjj_video_analysis` — BJJ video analysis tool
- Discover others: `github_list_repos()`

## Deployment Pipeline
- Push to `gelson12/super-agent` master → Railway auto-deploys `super-agent` service
- Push to same repo → also redeploys `obsidian-vault` service (same repo, different Dockerfile)
- No manual step needed — Railway webhook triggers on every push

## Domain
- `bridge-digital-solution.com` → served from `super-agent/website/index.html`
- Railway public URL for super-agent API: check `railway_list_services` for current URL

## Cloudinary (media storage)
- Upload bucket: `super_agent_builds` (APK builds)
- Transformation: raw upload for binary files, image for photos
- Public URL format: `https://res.cloudinary.com/<cloud_name>/raw/upload/<public_id>`
