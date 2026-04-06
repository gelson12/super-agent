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
# Store as CLAUDE_SESSION_TOKEN in Railway: base64 -w0 ~/.claude/credentials.json
# After claude login run: base64 -w0 ~/.claude/credentials.json  (Linux)
#                      or: base64 -i ~/.claude/credentials.json | tr -d '\n'  (macOS)
if [ -n "$CLAUDE_SESSION_TOKEN" ]; then
    mkdir -p /root/.claude
    echo "$CLAUDE_SESSION_TOKEN" | base64 -d > /root/.claude/.credentials.json
    chmod 600 /root/.claude/.credentials.json
    echo "[entrypoint] Claude.ai Pro credentials restored."

    # Verify the restored token is still valid — alert clearly if expired
    _auth_status=$(claude auth status 2>/dev/null || echo "{}")
    if echo "$_auth_status" | grep -q '"authMethod":"claude.ai"'; then
        _sub=$(echo "$_auth_status" | grep -o '"subscriptionType":"[^"]*"' | cut -d'"' -f4)
        echo "[entrypoint] Claude.ai Pro token VALID — authMethod=claude.ai subscriptionType=${_sub:-unknown}. Pro subscription active."
    elif echo "$_auth_status" | grep -q '"loggedIn":true'; then
        echo "[entrypoint] WARNING: Claude logged in but authMethod is not claude.ai (using API key fallback). Re-run 'claude login' in VS Code terminal and update CLAUDE_SESSION_TOKEN."
    else
        echo "[entrypoint] WARNING: CLAUDE_SESSION_TOKEN EXPIRED — Pro unavailable, falling back to ANTHROPIC_API_KEY."
        echo "[entrypoint] TO REFRESH: VS Code terminal → 'claude login' → approve in browser → 'cat /root/.claude/.credentials.json | base64 -w0' → update CLAUDE_SESSION_TOKEN in Railway Variables."
    fi
else
    echo "[entrypoint] WARNING: CLAUDE_SESSION_TOKEN not set — claude CLI will fall back to ANTHROPIC_API_KEY."
    echo "[entrypoint] TO ENABLE PRO: open VS Code terminal → run 'claude login' → approve in browser → run 'base64 -w0 /root/.claude/credentials.json' → add as CLAUDE_SESSION_TOKEN in Railway Variables."
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
mkdir -p /root/.local/share/code-server/User/globalStorage/storage.json 2>/dev/null || true
mkdir -p /root/.local/share/code-server/User/globalStorage
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

# ── Start all services via supervisor ─────────────────────────────────────────
echo "[entrypoint] Starting supervisor (nginx + uvicorn + code-server) on PORT=${PORT}"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
