#!/bin/bash
set -e

echo "[obsidian] Starting Obsidian vault MCP service..."

# ── First-boot vault seed ──────────────────────────────────────────────────────
if [ ! -f /vault/.obsidian/community-plugins.json ]; then
    echo "[obsidian] First boot — seeding vault config..."
    mkdir -p /vault/.obsidian
    cp -r /vault-seed/.obsidian/. /vault/.obsidian/
    echo "[obsidian] Vault config seeded."
else
    echo "[obsidian] Existing vault found — skipping seed."
fi

# ── Register vault with Obsidian ───────────────────────────────────────────────
# Without obsidian.json, Obsidian opens to the welcome/onboarding screen
# instead of loading the vault. This tells it to open /vault on startup.
mkdir -p /root/.config/obsidian
cat > /root/.config/obsidian/obsidian.json << 'VAULTJSON'
{
  "vaults": {
    "vault1": {
      "path": "/vault",
      "ts": 1744000000000,
      "open": true
    }
  }
}
VAULTJSON
echo "[obsidian] Vault registered at /vault."

# ── Start virtual display ──────────────────────────────────────────────────────
echo "[obsidian] Starting Xvfb virtual display on :99..."
Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX -noreset &
XVFB_PID=$!
sleep 3

# ── Launch Obsidian ────────────────────────────────────────────────────────────
echo "[obsidian] Launching Obsidian..."
DISPLAY=:99 /obsidian/obsidian \
    --no-sandbox \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage &
OBSIDIAN_PID=$!

# ── Wait for MCP WebSocket to be ready (up to 60s after update) ───────────────
echo "[obsidian] Waiting for Claude Code MCP plugin to start on port 22360..."
for i in $(seq 1 60); do
    if nc -z localhost 22360 2>/dev/null; then
        echo "[obsidian] MCP WebSocket server is UP on port 22360."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[obsidian] WARNING: MCP WebSocket did not start within 60s — plugin may need manual activation."
    fi
    sleep 1
done

# ── Keep container alive ───────────────────────────────────────────────────────
while true; do
    if ! kill -0 $OBSIDIAN_PID 2>/dev/null; then
        echo "[obsidian] Obsidian process died — restarting..."
        DISPLAY=:99 /obsidian/obsidian \
            --no-sandbox \
            --disable-gpu \
            --disable-software-rasterizer \
            --disable-dev-shm-usage &
        OBSIDIAN_PID=$!
        sleep 5
    fi
    sleep 10
done
