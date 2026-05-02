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

    # ── Service registry (I2 — replaces hardcoded hostnames) ─────────────────
    # Single source of truth for the URLs of sibling Railway services.
    # Default values match the production layout; override per env via
    # CLI_WORKER_URL / OBSIDIAN_MCP_URL / LEGION_BASE_URL / N8N_BASE_URL etc.
    # Use config.service_url("name") to read at call-sites instead of hardcoding.
    obsidian_mcp_url: str = "http://obsidian-vault.railway.internal:22360/sse"
    inspiring_cat_url: str = "https://inspiring-cat-production.up.railway.app"
    legion_base_url: str = ""              # set via LEGION_BASE_URL when distributed Haiku is on
    legion_api_shared_secret: str = ""

    # v0.dev (Vercel AI) — website / UI component generation
    # Set via Railway env var V0_API_KEY
    # Obtain at: https://v0.dev → Settings → API Keys
    v0_api_key: str = ""

    # Multi-framework orchestration (LangGraph custom graphs, AutoGen, CrewAI)
    # Global kill switch for the /chat/graph, /chat/crew, /chat/groupchat endpoints.
    frameworks_enabled: bool = True
    # PostgresSaver DSN for LangGraph checkpointing — falls back to database_url.
    langgraph_checkpointer_dsn: str = ""
    # AutoGen group-chat termination cap (messages).
    autogen_max_turns: int = 12
    # CrewAI process model — "sequential" or "hierarchical".
    crewai_process: str = "hierarchical"


settings = Settings()


# ── Service registry helpers (I2) ─────────────────────────────────────────────
# Logical service-name → URL lookup. New code should call service_url("name")
# instead of hardcoding hostnames. Old call sites can migrate gradually.

_SERVICE_REGISTRY = {
    "cli_worker":     lambda: settings.cli_worker_url or settings.inspiring_cat_url,
    "inspiring_cat":  lambda: settings.inspiring_cat_url,
    "obsidian_vault": lambda: settings.obsidian_mcp_url,
    "n8n":            lambda: settings.n8n_base_url,
    "legion":         lambda: settings.legion_base_url,
    "self":           lambda: (
        f"https://{settings.railway_public_domain}"
        if settings.railway_public_domain else ""
    ),
}


def service_url(name: str) -> str:
    """Resolve a logical service name to a URL. Returns '' if unknown/unset."""
    fn = _SERVICE_REGISTRY.get(name)
    return fn() if fn else ""


def list_services() -> dict[str, str]:
    """Snapshot of all known services and their resolved URLs (for /admin/services)."""
    return {name: fn() for name, fn in _SERVICE_REGISTRY.items()}
