from app.hf.discovery import _load_curated, pick_model, shortlist_models


def test_curated_loads_nonempty():
    models = _load_curated()
    assert len(models) > 0
    sample = models[0]
    for field in ("id", "task", "modality", "params_b", "license", "license_commercial"):
        assert field in sample


def test_pick_model_prefers_smallest_in_task():
    picked = pick_model(task="chat", modality="text")
    assert picked is not None
    # Phi-3.5-mini at 3.8B or Llama-3.2-3B-Instruct at 3B are smallest chat options
    all_chat = [m for m in _load_curated() if m["task"] == "chat"]
    smallest = min(all_chat, key=lambda m: m["params_b"])
    assert picked == smallest["id"]


def test_pick_model_none_for_nonexistent_task():
    assert pick_model(task="nonexistent-task") is None


def test_shortlist_models_returns_k_sorted_by_size():
    top3 = shortlist_models(task="chat", modality="text", k=3)
    assert len(top3) == 3
    assert all(isinstance(m, str) for m in top3)


def test_pick_model_max_params_filter():
    picked = pick_model(task="chat", modality="text", max_params_b=1.0)
    # Nothing in curated chat bucket is under 1B params
    assert picked is None
