FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=20 \
    PATH="/root/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git supervisor nginx \
      build-essential libpq-dev gettext-base openssh-client \
      procps psmisc zstd \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
      libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
      libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-audit --no-fund \
      @anthropic-ai/claude-code \
      @google/gemini-cli \
    && npm cache clean --force

# Kimi CLI removed 2026-04-24: Moonshot made `kimi` CLI paid-subscription-only
# (402 Payment Required on login). uv is kept because it's still useful for
# pip-alternative workflows and is small (~5 MB).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -sf /root/.local/bin/uv  /usr/local/bin/uv  \
    && ln -sf /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv --version

# Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e .

# Playwright browsers (Chromium for L4/L5; camoufox installed via python-playwright for L4)
RUN python -m playwright install --with-deps chromium \
    && pip install camoufox[geoip] && python -m camoufox fetch || true

COPY app /app/app
COPY migrations /app/migrations
COPY scripts /app/scripts
RUN chmod +x /app/scripts/*.sh 2>/dev/null || true
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY nginx.conf /etc/nginx/nginx.conf
COPY legion_config.yaml /app/legion_config.yaml

RUN mkdir -p /workspace/legion/{claude-b,kimi,ollama,hf_cache,diag,cdp-profile-b}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
