FROM python:3.12-slim

RUN pip install --no-cache-dir "mcp>=1.0.0" "uvicorn>=0.27.0" "starlette>=0.36.0"

# Seed the vault with a welcome note
RUN mkdir -p /vault-seed
RUN printf '# Super Agent Knowledge Vault\n\nThis vault is the persistent knowledge base for Super Agent.\n\n## Purpose\n- Store self-improvement insights and improvement reports\n- Save architecture decisions and agent behaviour notes\n- Provide inspiration for the self-improvement pipeline\n\n## Usage\nAll Super Agent models (Haiku, Sonnet, Opus) and Claude Code CLI can read and write notes here via MCP tools.\n' \
    > /vault-seed/Welcome.md

COPY obsidian-vault/vault_mcp_server.py /vault_mcp_server.py
COPY obsidian-vault/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22360

CMD ["/entrypoint.sh"]
