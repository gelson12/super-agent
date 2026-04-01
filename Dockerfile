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

# ── Railway CLI ───────────────────────────────────────────────────────────────
RUN npm install -g @railway/cli

# ── VS Code extensions (GitHub + GitLens + Claude) ────────────────────────────
RUN code-server --install-extension GitHub.vscode-pull-request-github \
    && code-server --install-extension eamodio.gitlens \
    && code-server --install-extension Anthropic.claude-code \
    && echo "[docker] VS Code extensions installed."

# ── Java + Android tools (minimal — only what APK builds need) ───────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jdk-headless \
    unzip xz-utils zip \
    && rm -rf /var/lib/apt/lists/*

# Resolve JAVA_HOME dynamically so it works regardless of JDK version
RUN JAVA_BIN=$(readlink -f $(which java)) \
    && echo "JAVA_HOME=$(dirname $(dirname $JAVA_BIN))" >> /etc/environment
ENV JAVA_HOME=/usr/lib/jvm/default-java

# ── Flutter SDK (no precache — first flutter build downloads what it needs) ──
ENV FLUTTER_VERSION=3.27.4
ENV FLUTTER_HOME=/opt/flutter
RUN curl -fsSL --retry 3 --retry-delay 5 \
    "https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz" \
    | tar xJ -C /opt/ \
    && git config --global --add safe.directory /opt/flutter \
    && flutter config --no-analytics \
    && echo "[docker] Flutter ${FLUTTER_VERSION} installed."
ENV PATH="${FLUTTER_HOME}/bin:${PATH}"

# ── Android SDK command-line tools (platforms + build-tools only, skip NDK) ──
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools \
    && curl -fsSL --retry 3 --retry-delay 5 \
       https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
       -o /tmp/cmdtools.zip \
    && unzip -q /tmp/cmdtools.zip -d /tmp/cmdtools \
    && mv /tmp/cmdtools/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest \
    && rm -rf /tmp/cmdtools /tmp/cmdtools.zip \
    && yes | sdkmanager --licenses \
    && sdkmanager "platforms;android-34" "build-tools;34.0.0" "platform-tools" \
    && flutter config --android-sdk ${ANDROID_HOME} \
    && echo "[docker] Android SDK ready."
ENV PATH="${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${PATH}"

# ── VS Code / code-server extensions (Dart + Flutter) ────────────────────────
RUN code-server --install-extension Dart-Code.dart-code \
    && code-server --install-extension Dart-Code.flutter \
    && echo "[docker] Dart + Flutter extensions installed."

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
