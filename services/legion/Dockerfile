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
      procps psmisc \
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

# Kimi CLI install deferred to P2 (when KIMI_ENABLED flips on).
# Current P0/P1 image does not need kimi/uv at runtime because KIMI_ENABLED=false
# gates every Kimi code path. Skipping this install shaves ~200MB off the image
# and removes the most fragile step (upstream installer has quirky PATH/hash
# behavior with `command -v uv` in non-interactive shells).
# When re-enabling: install uv first, then kimi via bash -c 'hash -r && ...'.

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
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY nginx.conf /etc/nginx/nginx.conf
COPY legion_config.yaml /app/legion_config.yaml

RUN mkdir -p /workspace/legion/{claude-b,kimi,ollama,hf_cache,diag,cdp-profile-b}

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
