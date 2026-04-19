from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM API keys
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""

    # Cost guardrails — max tokens per call
    max_tokens_claude: int = 1200
    max_tokens_gemini: int = 2048
    max_tokens_deepseek: int = 2048

    # Web UI access password (leave empty to disable auth)
    ui_password: str = ""

    # Owner safe word — required to authorize critical write operations
    # (GitHub writes, n8n workflow edits, shell write commands)
    # MUST be set via Railway env var OWNER_SAFE_WORD. Empty string disables
    # safe-word protection entirely — do not leave unset in production.
    owner_safe_word: str = ""

    # Red team / adversarial challenge mode
    # When True, Haiku attacks every response (complexity >= 3) looking for flaws.
    # Off by default — enable via Railway env var CONFIDENCE_MODE=true.
    confidence_mode: bool = False

    # code-server (VSCode in browser) password — set via CODE_SERVER_PASSWORD
    code_server_password: str = ""

    # GitHub integration
    github_pat: str = ""

    # Cloudinary storage
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    cloudinary_cloud_name: str = ""

    # LangSmith observability (optional)
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "super-agent"

    # n8n workflow automation — set via Railway env vars
    n8n_base_url: str = ""   # e.g. https://n8n-production.up.railway.app
    n8n_api_key: str = ""    # n8n → Settings → n8n API → Create API Key

    # Railway CLI token — set via Railway env var RAILWAY_TOKEN
    railway_token: str = ""

    # PostgreSQL — injected automatically by Railway when PostgreSQL plugin is added.
    # Falls back to SQLite in /workspace if not set.
    # Railway sets DATABASE_URL as "postgres://..." — we normalise to "postgresql://..."
    database_url: str = ""

    # Tavily web search (optional upgrade from DuckDuckGo — higher quality results)
    tavily_api_key: str = ""

    # Railway public URL — Railway auto-injects RAILWAY_PUBLIC_DOMAIN (no protocol prefix).
    # Used to build direct APK download links served from this container.
    # e.g. RAILWAY_PUBLIC_DOMAIN=super-agent-production.up.railway.app
    railway_public_domain: str = ""

    # CLI Worker service URL — the dedicated Railway service that runs Claude/Gemini CLI.
    # Set this to the cli-worker Railway domain after deploying the cli-worker service.
    # e.g. CLI_WORKER_URL=https://cli-worker-production.up.railway.app
    # If not set, falls back to direct subprocess (single-container / dev mode).
    cli_worker_url: str = ""

    # Bridge website — email notifications for form submissions
    # Use a Gmail App Password (not your regular Gmail password):
    #   Google Account → Security → 2-Step Verification → App passwords
    # Set these in Railway env vars: SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL
    smtp_user: str = ""        # e.g. bridge.digital.solution@gmail.com
    smtp_password: str = ""    # 16-char Gmail App Password
    notify_email: str = "bridge.digital.solution@gmail.com"  # recipient
    n8n_contact_webhook_url: str = ""  # set via N8N_CONTACT_WEBHOOK_URL Railway var


settings = Settings()
