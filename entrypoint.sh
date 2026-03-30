#!/bin/bash
set -e

export PORT=${PORT:-8000}

# ── Generate nginx config with correct PORT ────────────────────────────────────
envsubst '$PORT' < /app/nginx.conf.template > /etc/nginx/nginx.conf

# ── Git identity + GitHub PAT credential store ────────────────────────────────
if [ -n "$GITHUB_PAT" ]; then
    git config --global credential.helper store
    echo "https://x-access-token:${GITHUB_PAT}@github.com" > /root/.git-credentials
    git config --global user.email "super-agent@railway.app"
    git config --global user.name "Super Agent"
    echo "[entrypoint] GitHub credentials configured."
fi

# ── Workspace for repos ────────────────────────────────────────────────────────
mkdir -p /workspace /var/log/supervisor

# ── Start all services via supervisor ─────────────────────────────────────────
echo "[entrypoint] Starting supervisor (nginx + uvicorn + code-server) on PORT=${PORT}"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
