---
type: schema
tags: [meta, schema]
date: 2026-04-17
---

# Vault Frontmatter Schema

Every note written to this vault should include YAML frontmatter with at minimum `type`, `date`, and `tags`.
Use `obsidian_update_frontmatter` to patch metadata without touching the body.

## Required Fields (all note types)

| Field  | Type   | Description                        | Example               |
|--------|--------|------------------------------------|-----------------------|
| `type` | string | Note category (see types below)    | `type: decision`      |
| `date` | string | ISO-8601 date created              | `date: 2026-04-17`    |
| `tags` | list   | One or more classification tags    | `tags: [architecture, postgres]` |

## Note Types

| type | Folder | Used for |
|------|--------|----------|
| `decision`      | `Decisions/`       | Architecture choices, technology selections, approach decisions |
| `architecture`  | `Architecture/`    | System design, component descriptions, data flow docs |
| `improvement`   | `Improvements/`    | Self-improvement ideas, bugs fixed, optimisations shipped |
| `daily`         | `Conversations/`   | Daily session logs, conversation summaries |
| `incident`      | `Incidents/`       | Outages, bugs, post-mortems |
| `reference`     | `Reference/`       | Guides, API docs, how-tos |
| `schema`        | root / `_templates/` | Vault meta-documents (like this one) |

## Optional Fields by Type

### decision
```yaml
status: proposed | approved | rejected | superseded
impact: low | medium | high
supersedes: "[[OldDecision]]"
```

### architecture
```yaml
component: super-agent | obsidian-vault | inspiring-cat | n8n | postgres
status: current | planned | deprecated
```

### improvement
```yaml
category: routing | memory | security | ux | performance | analytics
shipped: true | false
commit: abc1234
```

### incident
```yaml
severity: p0 | p1 | p2
resolved: true | false
root_cause: "brief description"
```

## Naming Conventions

- `Decisions/YYYY-MM-DD-short-slug.md` — e.g. `Decisions/2026-04-17-use-pgvector-embeddings.md`
- `Architecture/ComponentName.md` — e.g. `Architecture/RoutingDispatcher.md`
- `Improvements/YYYY-MM-DD.md` — one file per day of improvements
- `Conversations/YYYY-MM-DD.md` — one file per session day
- `Incidents/YYYY-MM-DD-slug.md` — e.g. `Incidents/2026-04-16-cli-token-expired.md`
