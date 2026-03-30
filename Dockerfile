FROM python:3.12-slim

WORKDIR /app

# ── System packages ────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    git \
    curl \
    gnupg \
    ca-certificates \
    gettext-base \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── code-server (VSCode in browser) ───────────────────────────────────────────
RUN curl -fsSL https://code-server.dev/install.sh | sh \
    && which code-server \
    && code-server --version

# ── Claude Code CLI ───────────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Config files ──────────────────────────────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY nginx.conf.template /app/nginx.conf.template

# ── Workspace for cloned repos + code-server user dirs ───────────────────────
RUN mkdir -p /workspace /workspace/.vscode /workspace/.vscode-ext /var/log/supervisor

# ── Entrypoint (strip Windows CRLF → LF, then make executable) ───────────────
COPY entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
