FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# Xvfb + Electron runtime dependencies
RUN apt-get update && apt-get install -y \
    xvfb \
    wget \
    libgtk-3-0 \
    libnotify4 \
    libnss3 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    libatspi2.0-0 \
    libdrm2 \
    libgbm1 \
    libasound2 \
    libx11-xcb1 \
    libxcb-dri3-0 \
    && rm -rf /var/lib/apt/lists/*

# Download and extract Obsidian AppImage
# Extracting avoids the FUSE requirement in Docker containers
RUN wget -q "https://github.com/obsidianmd/obsidian-releases/releases/download/v1.8.10/Obsidian-1.8.10.AppImage" \
        -O /obsidian.AppImage \
    && chmod +x /obsidian.AppImage \
    && /obsidian.AppImage --appimage-extract \
    && mv squashfs-root /obsidian \
    && rm /obsidian.AppImage

# Pre-install Claude Code MCP plugin (Ian Sinnott) from GitHub releases
RUN mkdir -p /vault-seed/.obsidian/plugins/obsidian-claude-code-mcp \
    && wget -q "https://github.com/iansinnott/obsidian-claude-code-mcp/releases/latest/download/main.js" \
         -O /vault-seed/.obsidian/plugins/obsidian-claude-code-mcp/main.js \
    && wget -q "https://github.com/iansinnott/obsidian-claude-code-mcp/releases/latest/download/manifest.json" \
         -O /vault-seed/.obsidian/plugins/obsidian-claude-code-mcp/manifest.json \
    && (wget -q "https://github.com/iansinnott/obsidian-claude-code-mcp/releases/latest/download/styles.css" \
         -O /vault-seed/.obsidian/plugins/obsidian-claude-code-mcp/styles.css 2>/dev/null || true)

# Copy pre-configured vault settings (community-plugins.json, app.json, etc.)
COPY vault-config/obsidian-config/ /vault-seed/.obsidian/

# The vault is mounted as a volume so notes persist across container restarts
EXPOSE 22360

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
