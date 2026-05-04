#!/bin/bash
set -e

# CLI Worker service — nginx on $PORT, task API on 8003 (internal), VS Code on 3001 (internal)
# Use Railway-injected PORT if set; fall back to 8002 for local dev
export PORT=${PORT:-8002}

# ── Git identity + GitHub credentials (PAT / GITHUB_TOKEN / SSH key) ─────────
# Accept either GITHUB_PAT or the standard GITHUB_TOKEN name
_GH_TOKEN="${GITHUB_PAT:-${GITHUB_TOKEN:-}}"

if [ -n "$_GH_TOKEN" ]; then
    # HTTPS credential store — works for all git operations
    git config --global credential.helper store
    echo "https://x-access-token:${_GH_TOKEN}@github.com" > /root/.git-credentials
    chmod 600 /root/.git-credentials

    # Rewrite SSH remotes (git@github.com:) to HTTPS automatically
    # so repos cloned with SSH still push/pull correctly via token
    git config --global url."https://x-access-token:${_GH_TOKEN}@github.com/".insteadOf "git@github.com:"

    git config --global user.email "${GIT_EMAIL:-gelson_m@hotmail.com}"
    git config --global user.name  "${GIT_NAME:-Gelson Mascarenhas}"

    # gh CLI auth — lets you create PRs, open issues, etc from the terminal
    echo "$_GH_TOKEN" | gh auth login --with-token 2>/dev/null && \
        echo "[entrypoint] gh CLI authenticated ($(gh auth status 2>&1 | head -1))." || \
        echo "[entrypoint] WARNING: gh CLI auth failed — token may lack required scopes."

    echo "[entrypoint] GitHub HTTPS credentials + gh CLI configured."
else
    echo "[entrypoint] WARNING: No GITHUB_PAT or GITHUB_TOKEN set — git push disabled."
fi

# ── SSH key from env var (optional — base64-encoded private key) ─────────────
# Store as GITHUB_SSH_KEY in Railway: base64 -w0 ~/.ssh/id_ed25519
if [ -n "$GITHUB_SSH_KEY" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    echo "$GITHUB_SSH_KEY" | base64 -d > /root/.ssh/id_ed25519
    chmod 600 /root/.ssh/id_ed25519
    # Trust github.com host fingerprint — avoids interactive prompt
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
    # Generate public key so tools can inspect it
    ssh-keygen -y -f /root/.ssh/id_ed25519 > /root/.ssh/id_ed25519.pub 2>/dev/null || true
    echo "[entrypoint] SSH key installed ($(cat /root/.ssh/id_ed25519.pub | awk '{print $2}' | cut -c1-20)...)."
fi

# ── Railway CLI authentication ────────────────────────────────────────────────
# Newer Railway CLI reads RAILWAY_TOKEN env var automatically — no login command needed
if [ -n "$RAILWAY_TOKEN" ]; then
    # RAILWAY_TOKEN already in env — no re-export needed; verify it works silently
    if railway whoami >/dev/null 2>&1; then
        echo "[entrypoint] Railway CLI authenticated ($(railway whoami 2>/dev/null))."
    else
        echo "[entrypoint] WARNING: RAILWAY_TOKEN is set but Railway CLI auth failed — token may be expired."
    fi
else
    echo "[entrypoint] WARNING: RAILWAY_TOKEN not set — autonomous redeploy disabled."
fi

# ── Claude.ai Pro session token (credentials for claude CLI) ─────────────────
# STRATEGY: Try volume credentials first (may have been auto-refreshed by keeper).
# If volume check fails or times out, ALWAYS restore from env var.
# Never trust stale volume files — the token keeper may have failed silently.
mkdir -p /root/.claude

_claude_valid=false

# Step 1: Quick check of volume credentials (5s timeout to avoid boot hangs)
if [ -f /root/.claude/.credentials.json ]; then
    _auth_status=$(timeout 5 claude auth status 2>/dev/null || echo "{}")
    if echo "$_auth_status" | grep -q '"authMethod":"claude.ai"'; then
        _sub=$(echo "$_auth_status" | grep -o '"subscriptionType":"[^"]*"' | cut -d'"' -f4)
        echo "[entrypoint] Claude.ai Pro credentials VALID in volume (subscriptionType=${_sub:-unknown})."
        _claude_valid=true
    else
        echo "[entrypoint] Volume credentials invalid/expired/timed out — restoring from env var."
    fi
fi

# Step 2: If volume check failed, ALWAYS restore from env var
if [ "$_claude_valid" = "false" ] && [ -n "$CLAUDE_SESSION_TOKEN" ]; then
    echo "$CLAUDE_SESSION_TOKEN" | base64 -d > /root/.claude/.credentials.json
    chmod 600 /root/.claude/.credentials.json
    echo "[entrypoint] Claude credentials restored from CLAUDE_SESSION_TOKEN."

    # Verify restored token (5s timeout)
    _auth_status=$(timeout 5 claude auth status 2>/dev/null || echo "{}")
    if echo "$_auth_status" | grep -q '"authMethod":"claude.ai"'; then
        _sub=$(echo "$_auth_status" | grep -o '"subscriptionType":"[^"]*"' | cut -d'"' -f4)
        echo "[entrypoint] Claude.ai Pro token VALID (subscriptionType=${_sub:-unknown})."
        _claude_valid=true
    elif echo "$_auth_status" | grep -q '"loggedIn":true'; then
        echo "[entrypoint] WARNING: Claude logged in but not claude.ai auth. Re-run 'claude login'."
    else
        echo "[entrypoint] WARNING: CLAUDE_SESSION_TOKEN EXPIRED — Pro unavailable."
        touch /tmp/.claude_boot_sick   # signal Python app to mark CLI sick before first task
        echo "[entrypoint] FIX: VS Code → 'claude login' → approve → 'cat /root/.claude/.credentials.json | base64 -w0' → update CLAUDE_SESSION_TOKEN."
    fi
elif [ "$_claude_valid" = "false" ]; then
    echo "[entrypoint] WARNING: No CLAUDE_SESSION_TOKEN and no valid volume credentials."
    touch /tmp/.claude_boot_sick   # signal Python app to mark CLI sick before first task
fi

# ── Google Gemini CLI session token (free-tier Pro backup) ───────────────────
# Store as GEMINI_SESSION_TOKEN in Railway: base64 -w0 /root/.gemini/credentials.json
# After 'gemini auth login' in VS Code terminal, encode the credentials file and
# paste the output as GEMINI_SESSION_TOKEN in Railway Variables → redeploy.
mkdir -p /root/.gemini
_gemini_valid=false

# PRIMARY: restore from volume-persisted backup (survives container restarts
# without needing Railway API — the token keeper writes here every 4 hours).
_GEMINI_VOLUME_CREDS="/workspace/.gemini_credentials_backup.json"
if [ -f "$_GEMINI_VOLUME_CREDS" ] && [ ! -f /root/.gemini/credentials.json ]; then
    cp "$_GEMINI_VOLUME_CREDS" /root/.gemini/credentials.json
    chmod 600 /root/.gemini/credentials.json
    echo "[entrypoint] Gemini credentials restored from volume backup ($_GEMINI_VOLUME_CREDS)."
fi

# Check if volume credentials are already good — probe actual auth, not just binary presence
if [ -f /root/.gemini/credentials.json ]; then
    if timeout 8 gemini -p "ping" >/dev/null 2>&1; then
        echo "[entrypoint] Gemini CLI AVAILABLE and authenticated from volume credentials — skipping env var restore."
        _gemini_valid=true
    else
        echo "[entrypoint] Volume Gemini credentials present but auth probe failed — will restore from env var."
    fi
fi

# Fallback: restore from GEMINI_SESSION_TOKEN env var
if [ "$_gemini_valid" = "false" ] && [ -n "$GEMINI_SESSION_TOKEN" ]; then
    echo "$GEMINI_SESSION_TOKEN" | base64 -d > /root/.gemini/credentials.json
    chmod 600 /root/.gemini/credentials.json
    echo "[entrypoint] Gemini CLI credentials restored from GEMINI_SESSION_TOKEN env var."

    if timeout 8 gemini -p "ping" >/dev/null 2>&1; then
        echo "[entrypoint] Gemini CLI AVAILABLE and authenticated from env var credentials."
        _gemini_valid=true
    else
        echo "[entrypoint] WARNING: Gemini credentials restored from env var but auth probe failed — token may be expired."
        touch /tmp/.gemini_boot_sick
    fi
elif [ "$_gemini_valid" = "false" ]; then
    echo "[entrypoint] INFO: GEMINI_SESSION_TOKEN not set and no volume backup — Gemini CLI backup inactive."
    echo "[entrypoint] TO ENABLE: VS Code terminal → 'gemini auth login' → base64 -w0 /root/.gemini/credentials.json → add as GEMINI_SESSION_TOKEN in Railway."
fi

# ── Workspace for repos ────────────────────────────────────────────────────────
mkdir -p /workspace /workspace/.vscode /workspace/.vscode-ext /var/log/supervisor

# ── Auto-clone the super-agent repo into /workspace ──────────────────────────
# Uses HTTPS + token so it's always push-ready without any manual setup
if [ -n "$_GH_TOKEN" ] && [ -n "$GITHUB_REPO" ] && [ ! -d "/workspace/$(basename "$GITHUB_REPO" .git)" ]; then
    REPO_NAME=$(basename "$GITHUB_REPO" .git)
    echo "[entrypoint] Cloning ${GITHUB_REPO} → /workspace/${REPO_NAME} ..."
    git clone "https://x-access-token:${_GH_TOKEN}@github.com/${GITHUB_REPO}.git" \
        "/workspace/${REPO_NAME}" 2>&1 | tail -3 | sed 's/^/[entrypoint] /'
    echo "[entrypoint] Repo ready at /workspace/${REPO_NAME}"
elif [ -d "/workspace/super-agent/.git" ] && [ -n "$_GH_TOKEN" ]; then
    echo "[entrypoint] /workspace/super-agent already cloned — pulling latest..."
    git -C /workspace/super-agent pull --ff-only 2>&1 | tail -2 | sed 's/^/[entrypoint] /' || true
fi

# ── VS Code workspace settings (GitHub + Railway + n8n env vars in terminal) ──
cat > /workspace/.vscode/settings.json <<VSCODE
{
  "git.autofetch": true,
  "git.enableSmartCommit": true,
  "git.confirmSync": false,
  "github.gitAuthentication": true,
  "security.workspace.trust.enabled": false,
  "security.workspace.trust.startupPrompt": "never",
  "security.workspace.trust.banner": "never",
  "security.workspace.trust.emptyWindow": true,
  "extensions.ignoreRecommendations": true,
  "terminal.integrated.defaultProfile.linux": "bash",
  "terminal.integrated.env.linux": {
    "RAILWAY_TOKEN":      "${RAILWAY_TOKEN:-}",
    "GITHUB_PAT":         "${GITHUB_PAT:-}",
    "GITHUB_TOKEN":       "${GITHUB_TOKEN:-${GITHUB_PAT:-}}",
    "GH_TOKEN":           "${GITHUB_TOKEN:-${GITHUB_PAT:-}}",
    "ANTHROPIC_API_KEY":  "${ANTHROPIC_API_KEY:-}",
    "N8N_BASE_URL":       "${N8N_BASE_URL:-}",
    "N8N_API_KEY":        "${N8N_API_KEY:-}",
    "GIT_EMAIL":          "${GIT_EMAIL:-gelson_m@hotmail.com}",
    "GIT_NAME":           "${GIT_NAME:-Gelson Mascarenhas}"
  }
}
VSCODE
echo "[entrypoint] VS Code workspace settings written."

# ── code-server USER settings (theme, extensions, editor prefs — always restored on boot) ──
mkdir -p /root/.local/share/code-server/User
cat > /root/.local/share/code-server/User/settings.json <<USERSETTINGS
{
  "workbench.colorTheme": "Abyss",
  "workbench.startupEditor": "none",
  "editor.fontSize": 14,
  "editor.fontFamily": "'Fira Code', 'Cascadia Code', monospace",
  "editor.fontLigatures": true,
  "editor.formatOnSave": true,
  "editor.minimap.enabled": false,
  "terminal.integrated.fontSize": 13,
  "git.autofetch": true,
  "git.enableSmartCommit": true,
  "git.confirmSync": false,
  "github.gitAuthentication": true,
  "security.workspace.trust.enabled": false,
  "security.workspace.trust.startupPrompt": "never",
  "security.workspace.trust.banner": "never",
  "security.workspace.trust.emptyWindow": true,
  "extensions.autoUpdate": false,
  "extensions.ignoreRecommendations": true,
  "telemetry.telemetryLevel": "off",
  "workbench.tips.enabled": false,
  "workbench.welcomePage.walkthroughs.openOnInstall": false
}
USERSETTINGS
echo "[entrypoint] code-server user settings written (theme: Abyss)."

# ── Pre-trust /workspace so VS Code never shows "Do you trust the authors?" ──
# Writes directly to the trusted-workspaces storage file that code-server reads.
mkdir -p /root/.local/share/code-server/User/globalStorage
# Remove if a previous bad boot created storage.json as a directory
rm -rf /root/.local/share/code-server/User/globalStorage/storage.json 2>/dev/null || true
cat > /root/.local/share/code-server/User/globalStorage/storage.json <<TRUSTDB
{
  "security.workspace.trust": {
    "localFolders": {
      "/workspace": { "trustLevel": "trusted" },
      "/app": { "trustLevel": "trusted" }
    }
  }
}
TRUSTDB
echo "[entrypoint] Workspace trust pre-granted for /workspace and /app."

# ── code-server config (password via file — avoids supervisord env quoting issues) ──
if [ -z "$UI_PASSWORD" ]; then
    echo "[entrypoint] WARNING: UI_PASSWORD not set — VS Code will use the default 'changeme' password. Set UI_PASSWORD in Railway Variables."
fi
mkdir -p /root/.config/code-server
cat > /root/.config/code-server/config.yaml <<EOF
bind-addr: 0.0.0.0:3001
auth: password
password: "${UI_PASSWORD:-changeme}"
disable-update-check: true
disable-telemetry: true
EOF
echo "[entrypoint] code-server config written (auth: password)."

# ── Verify code-server binary is reachable ────────────────────────────────────
if ! command -v code-server >/dev/null 2>&1; then
    echo "[entrypoint] WARNING: code-server not found in PATH — trying /usr/lib/code-server/bin/code-server"
fi
echo "[entrypoint] code-server location: $(which code-server 2>/dev/null || echo 'NOT FOUND')"

# ── Flutter & Android SDK health check (opt-in — slow, skip on normal boots) ──
# Set RUN_FLUTTER_DOCTOR=1 in Railway Variables to enable on next deploy.
if [ -n "$RUN_FLUTTER_DOCTOR" ] && command -v flutter >/dev/null 2>&1; then
    yes | sdkmanager --licenses >/dev/null 2>&1 || true
    flutter doctor --android-licenses -y >/dev/null 2>&1 || true
    flutter doctor 2>&1 | head -20 | sed 's/^/[flutter] /'
    echo "[entrypoint] Flutter ready: $(flutter --version 2>&1 | head -1)"
elif command -v flutter >/dev/null 2>&1; then
    echo "[entrypoint] Flutter found (set RUN_FLUTTER_DOCTOR=1 to run health check): $(flutter --version 2>&1 | head -1)"
else
    echo "[entrypoint] WARNING: Flutter not found in PATH — mobile builds unavailable"
fi

# ── Process bridge logo — remove white glow background ───────────────────────
if [ -f /app/static/bridge.jpg ] && [ ! -f /app/static/bridge.png ]; then
    python /app/remove_bg.py && echo "[entrypoint] Bridge logo processed."
elif [ -f /app/static/bridge.png ]; then
    echo "[entrypoint] Bridge logo already processed (bridge.png exists)."
else
    echo "[entrypoint] WARNING: bridge.jpg not found — logo will use fallback."
fi

# ── Claude Code CLI: n8n MCP server + CLAUDE.md ───────────────────────────────
# Gives `claude -p "..."` subprocess direct tool access to the n8n REST API
# so Claude can reason AND build workflows in a single call (no Python relay).

# 1. Write project .mcp.json so every `claude -p` from /workspace/super-agent
#    gets n8n, obsidian, and filesystem MCP servers automatically.
#    The n8n MCP server script lives in the repo at app/mcp/n8n_mcp_server.py.
_SA_DIR="/workspace/super-agent"
if [ -d "$_SA_DIR" ] && [ -n "$N8N_API_KEY" ]; then
    python3 - << 'MCPEOF'
import json, os, pathlib
sa = pathlib.Path('/workspace/super-agent')
sa.mkdir(parents=True, exist_ok=True)
cfg = {
    "mcpServers": {
        "n8n": {
            "command": "python3",
            "args": [str(sa / "app/mcp/n8n_mcp_server.py")],
            "env": {
                "N8N_BASE_URL": "https://outstanding-blessing-production-1d4b.up.railway.app",
                "N8N_API_KEY": os.environ.get("N8N_API_KEY", "")
            }
        },
        "obsidian": {
            "type": "sse",
            "url": os.environ.get("OBSIDIAN_MCP_URL", "http://obsidian-vault.railway.internal:22360/sse")
        },
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
            "env": {}
        }
    }
}
out = sa / ".mcp.json"
out.write_text(json.dumps(cfg, indent=2))
out.chmod(0o600)
print(f"[entrypoint] .mcp.json written to {out}")
MCPEOF
else
    echo "[entrypoint] INFO: Skipping .mcp.json write (super-agent dir not found or N8N_API_KEY not set)."
fi

# 1b. Also register via `claude mcp add` for sessions not run from /workspace/super-agent
if command -v claude >/dev/null 2>&1 && [ -f "/workspace/super-agent/app/mcp/n8n_mcp_server.py" ]; then
    claude mcp add n8n --stdio "python3 /workspace/super-agent/app/mcp/n8n_mcp_server.py" \
        -e N8N_BASE_URL="https://outstanding-blessing-production-1d4b.up.railway.app" \
        -e N8N_API_KEY="${N8N_API_KEY}" 2>/dev/null || true
    echo "[entrypoint] Claude CLI: n8n MCP server registered via claude mcp add."
elif command -v claude >/dev/null 2>&1 && [ -n "$N8N_API_KEY" ]; then
    echo "[entrypoint] INFO: n8n_mcp_server.py not found at /workspace/super-agent/app/mcp/ — skipping claude mcp add."
fi

# 1b. Register Obsidian MCP into ~/.claude.json (where Claude Code reads mcpServers from).
#     Use Python to safely merge — avoids overwriting existing auth credentials.
OBSIDIAN_MCP_URL="${OBSIDIAN_MCP_URL:-http://obsidian-vault.railway.internal:22360/sse}"
python3 - << PYEOF
import json, pathlib, os
p = pathlib.Path('/root/.claude.json')
cfg = {}
if p.exists():
    try:
        cfg = json.loads(p.read_text())
    except Exception:
        cfg = {}
cfg.setdefault('mcpServers', {})['obsidian'] = {
    'type': 'sse',
    'url': os.environ.get('OBSIDIAN_MCP_URL', 'http://obsidian-vault.railway.internal:22360/sse')
}
p.write_text(json.dumps(cfg, indent=2))
p.chmod(0o600)
print('[entrypoint] Obsidian MCP written to ~/.claude.json')
PYEOF

# 2. Write Claude Code CLI settings — pre-approve all tools so MCP calls never
#    pause to ask for user permission.
mkdir -p /root/.claude
cat > /root/.claude/settings.json << CLAUDESETTINGS
{
  "permissions": {
    "allow": [
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "Glob(*)",
      "Grep(*)",
      "mcp__*",
      "mcp__n8n__*",
      "mcp__n8n(*)",
      "mcp__obsidian__*",
      "mcp__obsidian(*)",
      "WebFetch(*)",
      "WebSearch(*)",
      "Agent(*)",
      "NotebookEdit(*)"
    ],
    "deny": []
  },
  "enableAllProjectMcpServers": true
}
CLAUDESETTINGS
chmod 600 /root/.claude/settings.json
echo "[entrypoint] Claude Code CLI settings written — obsidian MCP ($OBSIDIAN_MCP_URL) + all tools pre-approved."

# 3. Write CLAUDE.md to /workspace so every `claude -p` invocation inherits
#    the n8n API reference and workflow conventions automatically.
#    Guard: skip if a manually-edited copy exists (no auto-generated marker),
#    unless FORCE_CLAUDE_MD=1 is set.
mkdir -p /workspace
if [ -f /workspace/CLAUDE.md ] && ! grep -q "auto-generated on every boot" /workspace/CLAUDE.md && [ -z "$FORCE_CLAUDE_MD" ]; then
    echo "[entrypoint] CLAUDE.md already exists with manual edits — skipping overwrite (set FORCE_CLAUDE_MD=1 to force)."
else
cat > /workspace/CLAUDE.md <<CLAUDEMD
# Super Agent — Claude Code CLI Context

## Your Role: n8n Workflow Architect

You are an expert n8n workflow architect with full MCP tool access to this
n8n instance. You do NOT guess node types or rely on a fixed list. You
**discover what is actually installed**, design the best solution, and build it.

---

## MANDATORY DESIGN PROCESS — follow this every time

### Step 1: Discover available nodes (ALWAYS first)
Before writing any workflow JSON, call the node discovery tools:

\`\`\`
search_node_types("outlook")      ← find nodes for a specific service
search_node_types("email")        ← find by capability
search_node_types("schedule")     ← find trigger types
list_node_types()                 ← see everything installed
\`\`\`

Never assume a node type name. Always verify it exists with search_node_types
or list_node_types before using it in workflow JSON.

### Step 2: Inspect node parameters
For each node you plan to use, call get_node_type_details to understand
exactly what parameters it accepts:

\`\`\`
get_node_type_details("n8n-nodes-base.microsoftOutlook")
get_node_type_details("n8n-nodes-base.scheduleTrigger")
\`\`\`

This tells you the exact parameter names and accepted values — use them
precisely. Wrong parameter names cause silent failures.

### Step 3: Design the architecture
Lay out the full workflow structure before building:
- TRIGGER: what starts the workflow?
- STEPS: what transformations or decisions happen?
- ACTIONS: what does it do? (send, save, notify, call API)
- OUTPUT: where does the result go?

Explain your design to the user in plain English before building.

### Step 4: Build in phases (never all at once)
1. \`create_workflow\` with skeleton: trigger node + ONE action node only
2. \`get_workflow\` to confirm the live ID and structure
3. \`update_workflow\` to add remaining nodes (max 5 nodes per update call)
4. Repeat step 3 until all nodes are added
5. \`activate_workflow\` to make it live
6. Report: name, ID, what it does, webhook URL if applicable

**Never put more than ~8 nodes in a single create_workflow call.**
**Always read back with get_workflow after create before any update.**

---

## MCP Tools Available

### Node Discovery (use these FIRST)
- \`search_node_types(keyword)\` — find nodes by service name or capability
- \`list_node_types(category?)\` — list all installed nodes, optionally filtered
- \`get_node_type_details(node_type_name)\` — get full parameter schema for a node

### Workflow Management
- \`list_workflows(active_only?)\` — list all workflows
- \`get_workflow(workflow_id)\` — get full workflow JSON
- \`create_workflow(workflow_json)\` — create a new workflow
- \`update_workflow(workflow_id, workflow_json)\` — update existing workflow
- \`delete_workflow(workflow_id)\` — delete a workflow
- \`activate_workflow(workflow_id)\` — enable triggers
- \`deactivate_workflow(workflow_id)\` — disable triggers

### Execution
- \`execute_workflow(workflow_id, input_data?)\` — run manually
- \`list_executions(workflow_id?, limit?, status?)\` — list recent runs
- \`get_execution(execution_id)\` — inspect result + debug failures

---

## n8n Instance
- Base URL: ${N8N_BASE_URL:-not set}
- API version: v1

---

## Workflow JSON Structure
\`\`\`json
{
  "name": "Workflow Name",
  "nodes": [
    {
      "id": "unique-uuid-string",
      "name": "Human Readable Name",
      "type": "n8n-nodes-base.EXACT_TYPE_FROM_SEARCH",
      "position": [250, 300],
      "parameters": {},
      "typeVersion": 1
    }
  ],
  "connections": {
    "Human Readable Name": {
      "main": [[{"node": "Next Node Name", "type": "main", "index": 0}]]
    }
  },
  "settings": {"executionOrder": "v1"}
}
\`\`\`

**The "type" field must be the exact string returned by search_node_types.**
**The "name" field in connections must exactly match the node's "name" field.**

---

## For AI steps inside workflows
Use an HTTP Request node pointing at Super Agent:
- URL: ${SUPER_AGENT_URL:-https://super-agent-production.up.railway.app}/chat
- Method: POST
- Body (JSON): \`{"message": "{{ \$json.input }}", "session_id": "n8n-auto"}\`

---

## Debugging Failed Executions
1. \`list_executions(workflow_id, limit=5, status="error")\`
2. \`get_execution(execution_id)\` — find the failed node and exact error
3. Fix the parameter or node type, then \`update_workflow\`

---

## File System
- /workspace — cloned repos, code, builds
- /workspace/CLAUDE.md — this file (auto-generated on every boot)
- /app — Super Agent Python application source

## Environment
- Platform: Railway (Docker container, europe-west4)
- Python: 3.12 | Node: 20
- Flutter: /opt/flutter | Android SDK: /opt/android-sdk

## Obsidian Knowledge Vault — MCP Tools (14 tools, registered as mcp__obsidian__*)
The vault MCP server runs at http://obsidian-vault.railway.internal:22360/sse and is
pre-registered in ~/.claude.json with all mcp__obsidian__* permissions pre-approved.

Available tools:
- mcp__obsidian__list_directory     — list markdown notes (pass path="" for all)
- mcp__obsidian__read_file          — read full note content
- mcp__obsidian__write_file         — create or overwrite a note
- mcp__obsidian__append_to_file     — append to note without overwriting
- mcp__obsidian__search_files       — full-text search across all notes
- mcp__obsidian__delete_file        — delete a note
- mcp__obsidian__get_vault_info     — vault stats (note count, size, folders)
- mcp__obsidian__list_folders       — list folder structure
- mcp__obsidian__get_recent_notes   — N most recently modified notes
- mcp__obsidian__get_vault_summary  — all notes grouped by folder with word counts
- mcp__obsidian__move_file          — move or rename a note
- mcp__obsidian__search_by_tag      — find notes by YAML frontmatter or inline #tag
- mcp__obsidian__get_note_metadata  — read YAML frontmatter only (fast, no full content)
- mcp__obsidian__archive_old_notes  — move old notes to Archive/YYYY-MM/ (use dry_run=true first)

Use the vault to: save improvement logs, record architecture decisions, store session context,
search prior work, and keep notes for future Claude sessions.
CLAUDEMD
echo "[entrypoint] CLAUDE.md written to /workspace."
fi  # end CLAUDE.md guard

# ── nginx config: routes /health /tasks → uvicorn:8003, / → code-server:3001 ──
envsubst '${PORT}' < /app/nginx.cli.conf.template > /etc/nginx/nginx.conf
echo "[entrypoint] nginx config written (PORT=${PORT} → VS Code:3001 + task API:8003)."

# ── Boot summary ─────────────────────────────────────────────────────────────
echo "=========================================================="
echo "[entrypoint] Boot complete at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "  Claude CLI:  ${_claude_valid}  | Gemini CLI: ${_gemini_valid}"
echo "  GitHub token:  ${_GH_TOKEN:+set}${_GH_TOKEN:-NOT SET}  | Railway token: ${RAILWAY_TOKEN:+set}${RAILWAY_TOKEN:-NOT SET}"
echo "  UI_PASSWORD:   ${UI_PASSWORD:+set}${UI_PASSWORD:-NOT SET (using changeme!)}"
echo "=========================================================="

# ── Start CLI worker services via supervisor ──────────────────────────────────
echo "[entrypoint] Starting CLI worker (nginx:${PORT} → VS Code:3001 + task API:8003)"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
