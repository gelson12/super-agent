#!/bin/bash
set -e

# Railway's internal mesh always connects to container port 8000.
# Hardcode this so nginx binds to 8000 regardless of Railway's $PORT injection.
export PORT=8000

# ── Generate nginx config with correct PORT ────────────────────────────────────
envsubst '$PORT' < /app/nginx.conf.template > /etc/nginx/nginx.conf

# ── Git identity + GitHub PAT credential store ────────────────────────────────
if [ -n "$GITHUB_PAT" ]; then
    git config --global credential.helper store
    echo "https://x-access-token:${GITHUB_PAT}@github.com" > /root/.git-credentials
    git config --global user.email "gelson_m@hotmail.com"
    git config --global user.name "Gelson Mascarenhas"
    echo "[entrypoint] GitHub credentials configured."
fi

# ── Railway CLI authentication ────────────────────────────────────────────────
if [ -n "$RAILWAY_TOKEN" ]; then
    railway login --token "${RAILWAY_TOKEN}" 2>/dev/null && \
        echo "[entrypoint] Railway CLI authenticated." || \
        echo "[entrypoint] WARNING: Railway CLI login failed — check RAILWAY_TOKEN."
fi

# ── Workspace for repos ────────────────────────────────────────────────────────
mkdir -p /workspace /workspace/.vscode /workspace/.vscode-ext /var/log/supervisor

# ── VS Code workspace settings (GitHub + Railway + n8n env vars in terminal) ──
cat > /workspace/.vscode/settings.json <<VSCODE
{
  "git.autofetch": true,
  "git.enableSmartCommit": true,
  "git.confirmSync": false,
  "github.gitAuthentication": true,
  "terminal.integrated.defaultProfile.linux": "bash",
  "terminal.integrated.env.linux": {
    "RAILWAY_TOKEN": "${RAILWAY_TOKEN:-}",
    "GITHUB_PAT": "${GITHUB_PAT:-}",
    "N8N_BASE_URL": "${N8N_BASE_URL:-}",
    "N8N_API_KEY": "${N8N_API_KEY:-}"
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
  "extensions.autoUpdate": false,
  "telemetry.telemetryLevel": "off",
  "workbench.tips.enabled": false
}
USERSETTINGS
echo "[entrypoint] code-server user settings written (theme: Abyss)."

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

# ── Start all services via supervisor ─────────────────────────────────────────
echo "[entrypoint] Starting supervisor (nginx + uvicorn + code-server) on PORT=${PORT}"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
