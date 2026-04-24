import pytest

from app.suitability import _heuristic, classify, shortlist


def test_heuristic_code_boosts_claude_kimi():
    scores = _heuristic("def foo(x): return x+1", ["claude_b", "kimi", "gemini_b", "hf"])
    assert scores["claude_b"] >= 0.7
    assert scores["kimi"] >= 0.7


def test_heuristic_short_chat_boosts_gemini_ollama():
    scores = _heuristic("what's the capital of France?", ["claude_b", "kimi", "gemini_b", "ollama"])
    assert scores["gemini_b"] >= 0.7
    assert scores["ollama"] >= 0.7


def test_shortlist_returns_topk():
    scores = {"a": 0.9, "b": 0.3, "c": 0.7, "d": 0.5}
    top = shortlist(scores, k=2)
    assert top == ["a", "c"]


def test_shortlist_caps_at_max_k():
    scores = {f"a{i}": 0.5 for i in range(10)}
    top = shortlist(scores, k=8, max_k=5)
    assert len(top) == 5


@pytest.mark.asyncio
async def test_classify_falls_back_to_heuristic_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY_HAIKU_CLASSIFIER", raising=False)
    scores = await classify("hello world", ["gemini_b", "kimi"])
    assert set(scores.keys()) == {"gemini_b", "kimi"}
    assert all(0.0 <= v <= 1.0 for v in scores.values())


@pytest.mark.asyncio
async def test_classify_empty_agent_list():
    assert await classify("x", []) == {}
