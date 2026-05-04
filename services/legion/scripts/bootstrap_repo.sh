#!/bin/bash
# bootstrap_repo.sh — Clone super-agent repo into /workspace/super-agent on LEGION boot.
# Runs once via supervisord (autorestart=false). Idempotent — pulls if already cloned.
set -euo pipefail

REPO_DIR="/workspace/super-agent"
REPO_URL="https://github.com/gelson12/super-agent.git"

# Configure git identity
git config --global user.email "gelson_m@hotmail.com"
git config --global user.name "Gelson Mascarenhas"
git config --global init.defaultBranch master

# Wire PAT-based authentication if GITHUB_PAT is set
if [ -n "${GITHUB_PAT:-}" ]; then
    git config --global credential.helper store
    echo "https://${GITHUB_PAT}:x-oauth-basic@github.com" > /root/.git-credentials
    # Rewrite SSH remotes to HTTPS so git@ URLs work transparently
    git config --global url."https://github.com/".insteadOf "git@github.com:"
    AUTH_URL="https://${GITHUB_PAT}:x-oauth-basic@github.com/gelson12/super-agent.git"
else
    AUTH_URL="${REPO_URL}"
fi

if [ -d "${REPO_DIR}/.git" ]; then
    echo "[bootstrap] Repo already cloned at ${REPO_DIR} — pulling latest..."
    cd "${REPO_DIR}"
    git pull --ff-only origin master 2>&1 || echo "[bootstrap] Pull failed (offline?), using cached copy"
else
    echo "[bootstrap] Cloning super-agent repo to ${REPO_DIR}..."
    mkdir -p "$(dirname "${REPO_DIR}")"
    git clone --depth=1 "${AUTH_URL}" "${REPO_DIR}" 2>&1 \
        || git clone --depth=1 "${REPO_URL}" "${REPO_DIR}" 2>&1 \
        || echo "[bootstrap] Clone failed — Claude B will run without project context"
fi

echo "[bootstrap] Done. Repo state:"
ls "${REPO_DIR}" 2>/dev/null | head -20 || echo "(directory not available)"
