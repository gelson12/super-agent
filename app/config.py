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


settings = Settings()
