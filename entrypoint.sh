#!/bin/bash
set -e

# Railway's internal mesh always connects to container port 8000.
# Hardcode this so nginx binds to 8000 regardless of Railway's $PORT injection.
export PORT=8000

# ── Generate nginx config with correct PORT ────────────────────────────────────
envsubst '$PORT' < /app/nginx.conf.template > /etc/nginx/nginx.conf

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
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
    # Generate public key so tools can inspect it
    ssh-keygen -y -f /root/.ssh/id_ed25519 > /root/.ssh/id_ed25519.pub 2>/dev/null || true
    echo "[entrypoint] SSH key installed ($(cat /root/.ssh/id_ed25519.pub | awk '{print $2}' | cut -c1-20)...)."
fi

# ── Railway CLI authentication ────────────────────────────────────────────────
# Newer Railway CLI reads RAILWAY_TOKEN env var automatically — no login command needed
if [ -n "$RAILWAY_TOKEN" ]; then
    export RAILWAY_TOKEN="${RAILWAY_TOKEN}"
    # Verify the token works silently
    if railway whoami >/dev/null 2>&1; then
        echo "[entrypoint] Railway CLI authenticated ($(railway whoami 2>/dev/null))."
    else
        echo "[entrypoint] WARNING: RAILWAY_TOKEN is set but Railway CLI auth failed — token may be expired."
    fi
else
    echo "[entrypoint] WARNING: RAILWAY_TOKEN not set — autonomous redeploy disabled."
fi

# ── Claude.ai Pro session token (credentials for claude CLI) ─────────────────
# IMPORTANT: only restore from env var if no valid credentials exist on disk.
# If a volume persists /root/.claude/, the CLI auto-refreshes its OAuth tokens —
# blindly overwriting with an older env var token was the main cause of auth failures.
mkdir -p /root/.claude

_CLI_FLAG_DIR="/workspace"
[ -w "/workspace" ] || _CLI_FLAG_DIR="/app"
_claude_valid=false

# Check if existing credentials are already valid (volume-persisted or from a previous boot)
if [ -f /root/.claude/.credentials.json ]; then
    _auth_status=$(timeout 15 claude auth status 2>/dev/null || echo "{}")
    if echo "$_auth_status" | grep -q '"authMethod":"claude.ai"'; then
        _sub=$(echo "$_auth_status" | grep -o '"subscriptionType":"[^"]*"' | cut -d'"' -f4)
        echo "[entrypoint] Claude.ai Pro credentials VALID on disk (authMethod=claude.ai subscriptionType=${_sub:-unknown}) — skipping env var restore."
        rm -f "${_CLI_FLAG_DIR}/.pro_cli_down" 2>/dev/null || true
        _claude_valid=true
    else
        echo "[entrypoint] Existing credentials invalid or expired — will restore from CLAUDE_SESSION_TOKEN."
    fi
fi

# Only write from env var if disk credentials are missing or broken
if [ "$_claude_valid" = "false" ] && [ -n "$CLAUDE_SESSION_TOKEN" ]; then
    echo "$CLAUDE_SESSION_TOKEN" | base64 -d > /root/.claude/.credentials.json
    echo "$CLAUDE_SESSION_TOKEN" | base64 -d > /root/.claude/credentials.json
    echo "$CLAUDE_SESSION_TOKEN" | base64 -d > /root/.claude.json
    chmod 600 /root/.claude/.credentials.json /root/.claude/credentials.json /root/.claude.json 2>/dev/null || true
    echo "[entrypoint] Claude.ai Pro credentials restored from CLAUDE_SESSION_TOKEN env var."

    _auth_status=$(timeout 15 claude auth status 2>/dev/null || echo "{}")
    if echo "$_auth_status" | grep -q '"authMethod":"claude.ai"'; then
        _sub=$(echo "$_auth_status" | grep -o '"subscriptionType":"[^"]*"' | cut -d'"' -f4)
        echo "[entrypoint] Claude.ai Pro token VALID — authMethod=claude.ai subscriptionType=${_sub:-unknown}. CLI ready."
        rm -f "${_CLI_FLAG_DIR}/.pro_cli_down" 2>/dev/null || true
        _claude_valid=true
    elif echo "$_auth_status" | grep -q '"loggedIn":true'; then
        echo "[entrypoint] WARNING: Claude logged in but NOT via claude.ai (API key mode). Pro subscription inactive."
        echo "$(date -u +%Y-%m-%dT%H:%M:%S)|600" > "${_CLI_FLAG_DIR}/.pro_cli_down"
    else
        echo "[entrypoint] WARNING: CLAUDE_SESSION_TOKEN INVALID OR EXPIRED."
        echo "[entrypoint] TO REFRESH: VS Code terminal → 'claude login' → approve browser → run:"
        echo "[entrypoint]   cat /root/.claude/.credentials.json | base64 -w0"
        echo "[entrypoint]   then update CLAUDE_SESSION_TOKEN in Railway Variables → redeploy."
        echo "$(date -u +%Y-%m-%dT%H:%M:%S)|600" > "${_CLI_FLAG_DIR}/.pro_cli_down"
    fi
elif [ "$_claude_valid" = "false" ]; then
    echo "[entrypoint] WARNING: No CLAUDE_SESSION_TOKEN set and no valid credentials on disk — CLI will fall back to ANTHROPIC_API_KEY."
    echo "[entrypoint] TO ENABLE PRO: VS Code terminal → 'claude login' → approve browser → 'cat /root/.claude/.credentials.json | base64 -w0' → add as CLAUDE_SESSION_TOKEN in Railway."
fi

# ── Google Gemini CLI session token (free-tier Pro backup) ───────────────────
# Store as GEMINI_SESSION_TOKEN in Railway: base64 -w0 /root/.gemini/credentials.json
# After 'gemini auth login' in VS Code terminal, encode the credentials file and
# paste the output as GEMINI_SESSION_TOKEN in Railway Variables → redeploy.
if [ -n "$GEMINI_SESSION_TOKEN" ]; then
    mkdir -p /root/.gemini
    echo "$GEMINI_SESSION_TOKEN" | base64 -d > /root/.gemini/credentials.json
    chmod 600 /root/.gemini/credentials.json
    echo "[entrypoint] Gemini CLI credentials restored."

    # Verify the CLI is alive (also triggers silent OAuth token refresh)
    if gemini --version >/dev/null 2>&1; then
        echo "[entrypoint] Gemini CLI AVAILABLE — free-tier backup active ($(gemini --version 2>/dev/null | head -1))."
    else
        echo "[entrypoint] WARNING: Gemini CLI not responding — check credentials or reinstall: npm install -g @google/gemini-cli"
    fi
else
    echo "[entrypoint] INFO: GEMINI_SESSION_TOKEN not set — Gemini CLI backup inactive."
    echo "[entrypoint] TO ENABLE: VS Code terminal → 'gemini auth login' → base64 -w0 /root/.gemini/credentials.json → add as GEMINI_SESSION_TOKEN in Railway."
fi

# ── Workspace for repos ────────────────────────────────────────────────────────
mkdir -p /workspace /workspace/.vscode /workspace/.vscode-ext /var/log/supervisor

# ── Auto-clone the super-agent repo into /workspace ──────────────────────────
# Uses HTTPS + token so it's always push-ready without any manual setup
if [ -n "$_GH_TOKEN" ] && [ -n "$GITHUB_REPO" ] && [ ! -d "/workspace/$(basename $GITHUB_REPO .git)" ]; then
    REPO_NAME=$(basename "$GITHUB_REPO" .git)
    echo "[entrypoint] Cloning ${GITHUB_REPO} → /workspace/${REPO_NAME} ..."
    git clone "https://x-access-token:${_GH_TOKEN}@github.com/${GITHUB_REPO}.git" \
        "/workspace/${REPO_NAME}" 2>&1 | tail -3 | sed 's/^/[entrypoint] /'
    echo "[entrypoint] Repo ready at /workspace/${REPO_NAME}"
elif [ -d "/workspace/super-agent/.git" ]; then
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
    "GIT_NAME":           "${GIT_NAME:-Gelson Mascarenhas}",
    "CLAUDE_SESSION_TOKEN": "${CLAUDE_SESSION_TOKEN:-}"
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

# ── Flutter & Android SDK health check ───────────────────────────────────────
if command -v flutter >/dev/null 2>&1; then
    yes | sdkmanager --licenses >/dev/null 2>&1 || true
    flutter doctor --android-licenses -y >/dev/null 2>&1 || true
    flutter doctor 2>&1 | head -20 | sed 's/^/[flutter] /'
    echo "[entrypoint] Flutter ready: $(flutter --version 2>&1 | head -1)"
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

# 1. Register the n8n MCP server with Claude CLI (idempotent — safe to rerun)
if command -v claude >/dev/null 2>&1 && [ -n "$N8N_BASE_URL" ] && [ -n "$N8N_API_KEY" ]; then
    claude mcp add n8n --stdio "python /app/mcp/n8n_mcp_server.py" 2>/dev/null || true
    echo "[entrypoint] Claude CLI: n8n MCP server registered (n8n_mcp_server.py)."
else
    echo "[entrypoint] INFO: Skipping n8n MCP registration (claude not found or N8N_BASE_URL/N8N_API_KEY not set)."
fi

# 2. Write Claude Code CLI settings — pre-approve all tools so MCP calls never
#    pause to ask for permission (--dangerously-skip-permissions blocked as root).
mkdir -p /root/.claude
cat > /root/.claude/settings.json <<'CLAUDESETTINGS'
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
echo "[entrypoint] Claude Code CLI settings written — all tools pre-approved (no permission prompts)."

# 3. Write CLAUDE.md to /workspace so every `claude -p` invocation inherits
#    the n8n API reference and workflow conventions automatically.
mkdir -p /workspace
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
- URL: https://super-agent-production.up.railway.app/chat
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
CLAUDEMD
echo "[entrypoint] CLAUDE.md written to /workspace."

# ── Start all services via supervisor ─────────────────────────────────────────
echo "[entrypoint] Starting supervisor (nginx + uvicorn + code-server) on PORT=${PORT}"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
