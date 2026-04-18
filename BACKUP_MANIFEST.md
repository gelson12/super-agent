# Last Known Good — System Backup
**Date:** 2026-04-18  
**Branch:** `backup/lkg-2026-04-18`  
**Tag:** `lkg-2026-04-18`

---

## System State at Backup

### n8n Workflows (34 total — all live exports)
Stored in `n8n/live_backup/` — these are the exact JSON payloads from the live n8n instance, not the repo source files.

| Workflow | ID | Active | Notes |
|---|---|---|---|
| Chief of Staff — Command Centre | 83ZQ9b5xReUaF6Ib | ✅ | |
| Chief of Staff — System Intelligence & Operations | yuydLBM2TFJxeEEs | ✅ | Fixed: gemini-2.5-flash → 1.5-flash |
| Chief of Staff — Executive Monitor | 3k3lsAxKymNX7027 | ✅ | Fixed: fetch() → helpers.httpRequest() |
| Secretary — Gmail Manager | N4IBlfTKan8Oq4tQ | ✅ | |
| Secretary — Outlook Email & Calendar Operations | nOawPhpTyNjPPiEb | ✅ | |
| Telegram Bot - Claude CLI Chat | b43gK8dovLi2Tqzi | ✅ | |
| Crypto Specialist Super Agent v3 | 7onbBjeUwHkSsuyc | ✅ | |
| Senior Digital Content Intelligence Analyst | CyiacnE5DIKGlmZz | ✅ | |
| Claude Verification Code Monitor | jun8CaMnNhux1iEY | ✅ | |
| Roll Call — Daily Team Check-In | AWcSUH73xPxeCRg3 | ✅ | |
| Layer2 GitHub Alert Receiver | asj4GUTfXPjBrWvZ | ✅ | |
| Universal Catch-All | sCHZhoyRgEZUaxtT | ✅ | |
| Finance Operations Controller | gi03iyYunCXPj0iW | ✅ | |
| Crypto Volatility Watchdog | dBrfEjCiJY7wgImW | ✅ | |
| Claude-Verification-Monitor | jxnZZwTqJ7naPKc6 | ✅ | |
| Crypto Daily Performance Report | rdjetGa1aegwIB4t | ✅ | |
| Crypto Outcome Tracker | Dz9P19r0h2P5Om77 | ✅ | |
| Social Media SEO Orchestrator | vTkmtypLZykHXIX9 | ✅ | |
| Crypto Dashboard Webhook | 1dj3uv3G6acSniJC | ✅ | |
| Health Monitor - Fixed | u0cyS73kZJWNNy8u | ❌ inactive | |
| Crypto Specialist Super Agent v3 (dupe) | MbGbRoyuXIj526Mx | ❌ inactive | |
| Simple Anthropic API Test | tibJnxXwAmLCrTOz | ❌ inactive | |
| Super-Agent-Health-Monitor | ke7YzsAmGerVWVVc | ❌ inactive | |
| Anthropic API Emergency Fallback | Ybk0RxHPw5TvBrbH | ❌ inactive | |
| Robust Health Check | yZckxfWsvugSBFZh | ❌ inactive | |
| Business Hub | reHgYvQF9VMxvG8l | ❌ inactive | |
| AI Finance Operations Assistant | AmnY0PjTiYiotwyN | ❌ inactive | |
| Anthropic API Health Check (Fixed) | vslQLgGGrODq01OF | ❌ inactive | |
| Simple Health Check | se9seKSqdpSluNjT | ❌ inactive | |
| Anthropic-to-DeepSeek Fallback Router | MlXi6gXt7SnYmQ4e | ❌ inactive | |
| Crypto Specialist Super Agent v3 (dupe2) | qRrFtlmJygHVOYmm | ❌ inactive | |
| Claude Verification Code Monitor (dupe) | C8WfeQJPt7qNJDFV | ❌ inactive | |
| Crypto Outcome Tracker (dupe) | BwICylqCcCA22T0f | ❌ inactive | |
| Crypto Specialist Super Agent v3 (dupe3) | u4j2JabixohFf4MN | ❌ inactive | |

---

## Infrastructure

| Service | URL | Platform |
|---|---|---|
| n8n | https://outstanding-blessing-production-1d4b.up.railway.app | Railway |
| Super Agent | https://super-agent-production.up.railway.app | Railway |
| Inspiring Cat (CLI) | https://inspiring-cat-production.up.railway.app | Railway |
| PostgreSQL | Railway — divine-contentment project | Railway |

---

## Fixes Applied on 2026-04-18 (included in this backup)

1. **Chief of Staff — System Intelligence** — All 4 Gemini nodes downgraded from `gemini-2.5-flash` (20 req/day free) to `gemini-1.5-flash` (1500 req/day free). Stopped the 429 rate-limit death loop.
2. **Chief of Staff — Executive Monitor** — `fetch()` not defined in n8n task runner sandbox. Replaced with `this.helpers.httpRequest()` in Scan Services and Telegram Alert nodes.
3. **Dashboard avatar** — Chief of Staff workflows now always render `👨🏾` (dark-brown male) via name-based override in `static/index.html`.

---

## How to Restore a Workflow

```bash
# Use the n8n API to reimport any workflow from this backup
curl -X POST https://outstanding-blessing-production-1d4b.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: <key>" \
  -H "Content-Type: application/json" \
  -d @n8n/live_backup/<workflow_file>.json
```
