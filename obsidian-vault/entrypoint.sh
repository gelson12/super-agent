#!/bin/bash
set -e

echo "[obsidian] Starting Obsidian vault MCP service..."

# ── First-boot vault seed ──────────────────────────────────────────────────────
# On first boot, copy the pre-baked .obsidian config (plugins, settings) into
# the mounted volume. On subsequent boots the user's vault content is preserved.
if [ ! -f /vault/.obsidian/community-plugins.json ]; then
    echo "[obsidian] First boot — seeding vault config..."
    mkdir -p /vault/.obsidian
    cp -r /vault-seed/.obsidian/. /vault/.obsidian/
    echo "[obsidian] Vault config seeded."
else
    echo "[obsidian] Existing vault found — skipping seed."
fi

# ── Start virtual display ──────────────────────────────────────────────────────
# Obsidian is an Electron app and requires a display.
# Xvfb provides a virtual framebuffer so no physical monitor is needed.
echo "[obsidian] Starting Xvfb virtual display on :99..."
Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX -noreset &
XVFB_PID=$!
sleep 3

# ── Launch Obsidian ────────────────────────────────────────────────────────────
# --no-sandbox       : required when running as root in containers
# --disable-gpu      : prevents GPU process crash in headless environment
# --disable-software-rasterizer : avoids swrast fallback which can hang
# --disable-dev-shm-usage : prevents /dev/shm exhaustion in constrained containers
echo "[obsidian] Launching Obsidian with vault at /vault..."
DISPLAY=:99 /obsidian/obsidian \
    --no-sandbox \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage \
    /vault &
OBSIDIAN_PID=$!

# ── Wait for MCP WebSocket to be ready ────────────────────────────────────────
echo "[obsidian] Waiting for Claude Code MCP plugin to start on port 22360..."
for i in $(seq 1 30); do
    if nc -z localhost 22360 2>/dev/null; then
        echo "[obsidian] MCP WebSocket server is UP on port 22360."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[obsidian] WARNING: MCP WebSocket did not start within 30s — plugin may need manual activation."
    fi
    sleep 1
done

# ── Keep container alive ───────────────────────────────────────────────────────
# If Obsidian crashes, restart it. The vault volume persists across restarts.
while true; do
    if ! kill -0 $OBSIDIAN_PID 2>/dev/null; then
        echo "[obsidian] Obsidian process died — restarting..."
        DISPLAY=:99 /obsidian/obsidian \
            --no-sandbox \
            --disable-gpu \
            --disable-software-rasterizer \
            --disable-dev-shm-usage \
            /vault &
        OBSIDIAN_PID=$!
        sleep 5
    fi
    sleep 10
done
