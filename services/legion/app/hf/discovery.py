from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger("legion.hf.discovery")

CURATED_PATH = Path(__file__).parent / "curated.yaml"


@lru_cache(maxsize=1)
def _load_curated() -> list[dict]:
    if not CURATED_PATH.exists():
        log.warning("curated.yaml missing at %s", CURATED_PATH)
        return []
    data = yaml.safe_load(CURATED_PATH.read_text()) or {}
    return data.get("models") or []


def pick_model(
    task: str,
    modality: str = "text",
    commercial_only: bool = True,
    max_params_b: float | None = None,
) -> str | None:
    models = [
        m for m in _load_curated()
        if m.get("task") == task and m.get("modality") == modality
    ]
    if commercial_only:
        models = [m for m in models if m.get("license_commercial")]
    if max_params_b is not None:
        models = [m for m in models if m.get("params_b", 0) <= max_params_b]
    if not models:
        return None
    # Prefer smaller → faster/cheaper within task bucket
    models.sort(key=lambda m: m.get("params_b", 1e9))
    return models[0].get("id")


def shortlist_models(task: str, modality: str = "text", k: int = 3) -> list[str]:
    models = [
        m for m in _load_curated()
        if m.get("task") == task
        and m.get("modality") == modality
        and m.get("license_commercial")
    ]
    models.sort(key=lambda m: m.get("params_b", 1e9))
    return [m.get("id") for m in models[:k] if m.get("id")]
