---
type: reference
tags: [github, patterns, repos, api]
date: 2026-04-17
---

# GitHub Patterns & Repo Reference

## Repo Structure (gelson12)
- **super-agent** (main app)
  - `app/` — FastAPI backend, agents, tools, routing
  - `website/index.html` — bridge-digital-solution.com website
  - `obsidian-vault/` — Obsidian vault MCP server + vault files
  - `static/` — Chat UI, observe.html dashboard
  - Default branch: `master`
- **bjj_video_analysis** — BJJ video analysis tool

## Common Operations

### Read then modify a file
```
1. github_read_file(repo_name, file_path, branch="master")
2. Apply changes to content
3. github_create_or_update_file(repo_name, file_path, content, message, branch)
```

### Website edit workflow
```
1. github_read_file("super-agent", "website/index.html")
2. Find ALL occurrences of target string (usually 2 — header + footer)
3. github_create_or_update_file("super-agent", "website/index.html", updated_content, "Update: ...")
4. Railway auto-redeploys service radiant-appreciation on push
```

### Discover repo structure
```
1. github_list_repos() — see all repos
2. github_list_files(repo_name, path="") — root contents
3. github_list_files(repo_name, path="app/") — subdirectory
```

### Branch workflow
```
1. github_create_branch(repo_name, branch_name, source="master")
2. github_create_or_update_file(..., branch=branch_name)
3. github_create_pull_request(repo_name, title, body, head=branch_name)
```

## Error Recovery
- **404 on file**: try `master` then `main` branch; verify path with `github_list_files`
- **401 Bad credentials**: GITHUB_PAT expired — check `railway_list_variables`
- **403 Rate limit**: wait 60s, check reset time with `run_shell_command("date")`

## Commit Message Convention
Use imperative tense: "Add ...", "Fix ...", "Update ...", "Remove ..."
Reference issue/PR numbers when relevant: "Fix #42: ..."

## Known File Locations
- Instagram links: `website/index.html` lines ~918 and ~1000
- Main routing logic: `app/routing/dispatcher.py`
- Agent system prompts: `app/agents/*.py`
- Vault MCP server: `obsidian-vault/vault_mcp_server.py`
