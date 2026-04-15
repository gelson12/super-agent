#!/bin/bash
# Seed vault on first boot then start the MCP server
if [ ! -f /vault/Welcome.md ]; then
    echo "[vault-mcp] First boot — seeding vault..."
    cp -r /vault-seed/. /vault/
fi
exec python /vault_mcp_server.py
