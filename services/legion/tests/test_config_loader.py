from pathlib import Path

from app.config_loader import HiveConfig, LegionConfig, load_config


def test_load_config_from_repo_yaml():
    # Point at the shipped legion_config.yaml next to the Dockerfile
    p = Path(__file__).parent.parent / "legion_config.yaml"
    # lru_cache key collides across tests; clear first
    load_config.cache_clear()
    cfg = load_config(p)
    assert isinstance(cfg, LegionConfig)
    assert isinstance(cfg.hive, HiveConfig)
    # Shipped yaml sets α+β+γ+δ+ε summing to ~1.0 (minus ε, which is subtractive)
    w = cfg.weights
    assert 0.9 <= (w.alpha_historical + w.beta_suitability + w.gamma_latency + w.delta_reliability) <= 1.1
    assert cfg.min_acceptable_score == 0.35


def test_load_config_missing_file_returns_defaults(tmp_path):
    load_config.cache_clear()
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert cfg.min_acceptable_score == 0.35
    assert cfg.hive.shortlist_k == 3
    assert cfg.hive.early_termination_confidence_min == 0.85
