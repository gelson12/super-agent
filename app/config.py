from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LEGION_ENABLED: bool = False
    L5_ENABLED: bool = False
    HIVE_EARLY_TERMINATION: bool = True
    HF_ENABLED: bool = False
    OLLAMA_ENABLED: bool = False
    KIMI_ENABLED: bool = False
    DUAL_ACCOUNT_ENABLED: bool = False

    PG_DSN: str | None = None
    LEGION_API_SHARED_SECRET: str | None = None
    PRIMARY_BEACON_SECRET: str | None = None

    PRIMARY_HEALTH_URL: str = "http://inspiring-cat:8003/auth/login-status"
    PRIMARY_PROBE_TIMEOUT_S: float = 1.5


settings = Settings()
