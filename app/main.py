import logging
import time

from fastapi import FastAPI

from app import __version__
from app.config import settings
from app.redact import install_root_filter

install_root_filter()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("legion")

app = FastAPI(title="Legion Engineer", version=__version__)
_STARTED_AT = time.time()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "uptime_s": int(time.time() - _STARTED_AT),
        "legion_enabled": settings.LEGION_ENABLED,
        "l5_enabled": settings.L5_ENABLED,
        "dual_account_enabled": settings.DUAL_ACCOUNT_ENABLED,
    }


@app.get("/health/detailed")
def health_detailed() -> dict:
    return {
        **health(),
        "agents": {
            "kimi": settings.KIMI_ENABLED,
            "ollama": settings.OLLAMA_ENABLED,
            "hf": settings.HF_ENABLED,
        },
        "pg_configured": bool(settings.PG_DSN),
    }
