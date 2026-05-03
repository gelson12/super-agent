from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.rank import RankWeights

log = logging.getLogger("legion.config")

DEFAULT_CONFIG_PATH = Path("/app/legion_config.yaml")


@dataclass
class HiveConfig:
    shortlist_k: int = 3
    shortlist_max: int = 5
    deadlines_ms: dict[str, int] = field(default_factory=dict)
    early_termination_confidence_min: float = 0.85
    early_termination_latency_fraction_max: float = 0.4


_DEFAULT_REFINEMENT_CFG: dict = {
    "enabled": True,
    "complexity_min": 3,          # only refine complexity >= this
    "critique_enabled": True,
    "critique_deadline_ms": 6000,
    "refine_deadline_ms": 10000,
    "cot_enabled": True,
    "cot_deadline_ms": 15000,     # total budget for CoT reasoning + re-answer
}


@dataclass
class LegionConfig:
    weights: RankWeights = field(default_factory=RankWeights)
    hive: HiveConfig = field(default_factory=HiveConfig)
    min_acceptable_score: float = 0.35
    cold_start_sample_threshold: int = 30
    modality_priors: dict[str, dict[str, float]] = field(default_factory=dict)
    circuit_cooldown_s: dict[str, int] = field(default_factory=dict)
    circuit_error_threshold: int = 5
    refinement: dict = field(default_factory=lambda: dict(_DEFAULT_REFINEMENT_CFG))


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> LegionConfig:
    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        log.warning("legion_config.yaml not found at %s — using defaults", p)
        return LegionConfig()
    data = yaml.safe_load(p.read_text()) or {}

    ranking = data.get("ranking", {})
    w = ranking.get("weights", {})
    weights = RankWeights(
        alpha_historical=w.get("alpha_historical", 0.35),
        beta_suitability=w.get("beta_suitability", 0.25),
        gamma_latency=w.get("gamma_latency", 0.10),
        delta_reliability=w.get("delta_reliability", 0.15),
        epsilon_cost=w.get("epsilon_cost", 0.05),
        zeta_content_depth=w.get("zeta_content_depth", 0.10),
    )

    hive = data.get("hive", {})
    et = hive.get("early_termination", {})
    hive_cfg = HiveConfig(
        shortlist_k=hive.get("shortlist_k", 3),
        shortlist_max=hive.get("shortlist_max", 5),
        deadlines_ms=hive.get("deadlines_ms", {}),
        early_termination_confidence_min=et.get("confidence_min", 0.85),
        early_termination_latency_fraction_max=et.get("latency_fraction_max", 0.4),
    )

    circuit = data.get("circuit", {})
    refinement_raw = data.get("refinement", {})
    refinement_cfg = {**_DEFAULT_REFINEMENT_CFG, **refinement_raw}

    return LegionConfig(
        weights=weights,
        hive=hive_cfg,
        min_acceptable_score=ranking.get("min_acceptable_score", 0.35),
        cold_start_sample_threshold=ranking.get("cold_start_sample_threshold", 30),
        modality_priors=data.get("modality_priors", {}),
        circuit_cooldown_s=circuit.get("cooldown_s", {}),
        circuit_error_threshold=circuit.get("error_threshold", 5),
        refinement=refinement_cfg,
    )
