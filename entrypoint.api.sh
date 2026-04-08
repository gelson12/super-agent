#!/bin/bash
set -e

# API service — nginx on port 8000, uvicorn on 8001
export PORT=8000

# ── Generate nginx config ──────────────────────────────────────────────────────
envsubst '$PORT' < /app/nginx.conf.template > /etc/nginx/nginx.conf

# ── Git identity + GitHub credentials ─────────────────────────────────────────
_GH_TOKEN="${GITHUB_PAT:-${GITHUB_TOKEN:-}}"

if [ -n "$_GH_TOKEN" ]; then
    git config --global credential.helper store
    echo "https://x-access-token:${_GH_TOKEN}@github.com" > /root/.git-credentials
    chmod 600 /root/.git-credentials
    git config --global url."https://x-access-token:${_GH_TOKEN}@github.com/".insteadOf "git@github.com:"
    git config --global user.email "${GIT_EMAIL:-gelson_m@hotmail.com}"
    git config --global user.name  "${GIT_NAME:-Gelson Mascarenhas}"
    echo "$_GH_TOKEN" | gh auth login --with-token 2>/dev/null && \
        echo "[entrypoint.api] GitHub + gh CLI configured." || \
        echo "[entrypoint.api] WARNING: gh CLI auth failed."
else
    echo "[entrypoint.api] WARNING: No GITHUB_PAT/GITHUB_TOKEN — git push disabled."
fi

# ── SSH key ────────────────────────────────────────────────────────────────────
if [ -n "$GITHUB_SSH_KEY" ]; then
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    echo "$GITHUB_SSH_KEY" | base64 -d > /root/.ssh/id_ed25519
    chmod 600 /root/.ssh/id_ed25519
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
    ssh-keygen -y -f /root/.ssh/id_ed25519 > /root/.ssh/id_ed25519.pub 2>/dev/null || true
    echo "[entrypoint.api] SSH key installed."
fi

# ── Railway CLI ────────────────────────────────────────────────────────────────
if [ -n "$RAILWAY_TOKEN" ]; then
    export RAILWAY_TOKEN="${RAILWAY_TOKEN}"
    railway whoami >/dev/null 2>&1 && \
        echo "[entrypoint.api] Railway CLI authenticated ($(railway whoami 2>/dev/null))." || \
        echo "[entrypoint.api] WARNING: RAILWAY_TOKEN set but Railway CLI auth failed."
else
    echo "[entrypoint.api] WARNING: RAILWAY_TOKEN not set — autonomous redeploy disabled."
fi

# ── CLI_WORKER_URL check ───────────────────────────────────────────────────────
if [ -n "$CLI_WORKER_URL" ]; then
    echo "[entrypoint.api] CLI Worker URL configured: ${CLI_WORKER_URL}"
    echo "[entrypoint.api] Claude/Gemini CLI calls will be routed to the CLI worker service."
else
    echo "[entrypoint.api] WARNING: CLI_WORKER_URL not set — CLI calls will use direct subprocess fallback."
    echo "[entrypoint.api] TO ENABLE: add CLI_WORKER_URL=https://<cli-worker-domain> in Railway Variables."
fi

# ── Workspace dir ─────────────────────────────────────────────────────────────
mkdir -p /workspace /var/log/supervisor

# ── Bridge logo ───────────────────────────────────────────────────────────────
if [ -f /app/static/bridge.jpg ] && [ ! -f /app/static/bridge.png ]; then
    python /app/remove_bg.py && echo "[entrypoint.api] Bridge logo processed."
elif [ -f /app/static/bridge.png ]; then
    echo "[entrypoint.api] Bridge logo already processed."
fi

# ── Start API services via supervisor ─────────────────────────────────────────
echo "[entrypoint.api] Starting supervisor (nginx port ${PORT} + uvicorn port 8001)"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
