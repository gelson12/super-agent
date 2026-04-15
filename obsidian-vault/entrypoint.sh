#!/bin/bash
set -e

echo "[obsidian] Starting Obsidian vault MCP service..."

# ── First-boot vault seed ──────────────────────────────────────────────────────
if [ ! -f /vault/.obsidian/community-plugins.json ]; then
    echo "[obsidian] First boot — seeding vault config..."
    mkdir -p /vault/.obsidian
    cp -r /vault-seed/.obsidian/. /vault/.obsidian/
    # Create a seed note so vault is never empty (empty vault can prevent Obsidian opening)
    cat > /vault/Welcome.md << 'SEEDNOTE'
# Super Agent Knowledge Vault

This vault is the persistent knowledge base for Super Agent.

## Purpose
- Store self-improvement insights and improvement reports
- Save architecture decisions and agent behaviour notes
- Provide inspiration for the self-improvement pipeline

## Usage
Claude Code and all Super Agent models (Haiku, Sonnet, Opus) can read and write notes here via the Obsidian MCP plugin.
SEEDNODE
    echo "[obsidian] Vault config seeded."
else
    echo "[obsidian] Existing vault found — syncing critical config files..."
    # Always overwrite app.json and community-plugins.json from seed
    # so safeMode:false and plugin list are always correct even on old volumes
    cp /vault-seed/.obsidian/app.json /vault/.obsidian/app.json
    cp /vault-seed/.obsidian/community-plugins.json /vault/.obsidian/community-plugins.json
    # Ensure plugin data.json (port config) is present
    mkdir -p /vault/.obsidian/plugins/obsidian-claude-code-mcp
    cp /vault-seed/.obsidian/plugins/obsidian-claude-code-mcp/data.json \
       /vault/.obsidian/plugins/obsidian-claude-code-mcp/data.json
    # Copy plugin JS files if missing (e.g. volume predates plugin install)
    for f in main.js manifest.json styles.css; do
        src="/vault-seed/.obsidian/plugins/obsidian-claude-code-mcp/$f"
        dst="/vault/.obsidian/plugins/obsidian-claude-code-mcp/$f"
        [ -f "$src" ] && [ ! -f "$dst" ] && cp "$src" "$dst"
    done
fi

# ── Disable Obsidian auto-updates ─────────────────────────────────────────────
# Obsidian auto-downloads the latest .asar on every boot, which wastes ~30s
# and restarts the process unpredictably. Disable by pointing update URL to
# a dummy value via the app config directory.
mkdir -p /root/.config/obsidian
cat > /root/.config/obsidian/obsidian.json << 'VAULTJSON'
{
  "updateMode": "manual",
  "vaults": {
    "vault1": {
      "path": "/vault",
      "ts": 1744000000000,
      "open": true
    }
  }
}
VAULTJSON

# Obsidian also requires a per-vault JSON file named <vaultId>.json
cat > /root/.config/obsidian/vault1.json << 'VAULT1JSON'
{
  "path": "/vault",
  "ts": 1744000000000,
  "open": true
}
VAULT1JSON

echo "[obsidian] Vault registered at /vault. Auto-updates disabled."

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
    --disable-dev-shm-usage \
    --window-size=1280x800 &
OBSIDIAN_PID=$!

# ── Wait for MCP WebSocket (up to 90s — Obsidian takes ~30s to load plugins) ──
echo "[obsidian] Waiting for Claude Code MCP plugin on port 22360..."
for i in $(seq 1 90); do
    if nc -z localhost 22360 2>/dev/null; then
        echo "[obsidian] MCP WebSocket server is UP on port 22360. Ready."
        break
    fi
    if [ "$i" -eq 90 ]; then
        echo "[obsidian] WARNING: MCP WebSocket did not start within 90s."
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
            --disable-dev-shm-usage \
            --window-size=1280x800 &
        OBSIDIAN_PID=$!
        sleep 5
    fi
    sleep 10
done
