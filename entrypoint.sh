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
