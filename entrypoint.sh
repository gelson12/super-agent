#!/bin/bash
set -e

# Railway internal load balancer always connects to container port 8000.
# We hardcode 8000 here so Railway's mesh can always reach uvicorn,
# regardless of any PORT env var Railway may inject.
export PORT=8000

# ── Git identity + GitHub PAT credential store ────────────────────────────────
if [ -n "$GITHUB_PAT" ]; then
    git config --global credential.helper store
    echo "https://x-access-token:${GITHUB_PAT}@github.com" > /root/.git-credentials
    git config --global user.email "gelson_m@hotmail.com"
    git config --global user.name "Gelson Mascarenhas"
    echo "[entrypoint] GitHub credentials configured."
fi

# ── Workspace for cloned repos / persistent data ──────────────────────────────
mkdir -p /workspace

# ── Start FastAPI directly on port 8000 ──────────────────────────────────────
echo "[entrypoint] Starting uvicorn on PORT=${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
