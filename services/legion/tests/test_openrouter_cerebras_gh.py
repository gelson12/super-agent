"""Smoke tests for the three OpenAI-compatible free agents."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.cerebras import CerebrasAgent
from app.agents.github_models import GitHubModelsAgent
from app.agents.openrouter import OpenRouterAgent


def _mock_chat(content: str):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": 50},
    })
    return r


def _fake_async_client(response):
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── OpenRouter ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openrouter_disabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "false")
    r = await OpenRouterAgent().respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "disabled"


@pytest.mark.asyncio
async def test_openrouter_no_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    r = await OpenRouterAgent().respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "no_api_key"


@pytest.mark.asyncio
async def test_openrouter_happy_path(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    a = OpenRouterAgent()
    with patch("app.agents.openrouter.httpx.AsyncClient",
               return_value=_fake_async_client(_mock_chat("openrouter says hi"))):
        r = await a.respond("hi", 5000)
    assert r.success is True
    assert r.content == "openrouter says hi"
    assert r.cost_cents == 0.0


# ── Cerebras ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cerebras_disabled(monkeypatch):
    monkeypatch.setenv("CEREBRAS_ENABLED", "false")
    r = await CerebrasAgent().respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "disabled"


@pytest.mark.asyncio
async def test_cerebras_happy_path(monkeypatch):
    monkeypatch.setenv("CEREBRAS_ENABLED", "true")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-test")
    a = CerebrasAgent()
    with patch("app.agents.cerebras.httpx.AsyncClient",
               return_value=_fake_async_client(_mock_chat("cerebras says hi"))):
        r = await a.respond("hi", 5000)
    assert r.success is True
    assert r.content == "cerebras says hi"


# ── GitHub Models ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_github_models_disabled(monkeypatch):
    monkeypatch.setenv("GITHUB_MODELS_ENABLED", "false")
    r = await GitHubModelsAgent().respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "disabled"


@pytest.mark.asyncio
async def test_github_models_falls_back_to_github_pat(monkeypatch):
    monkeypatch.setenv("GITHUB_MODELS_ENABLED", "true")
    monkeypatch.delenv("GITHUB_MODELS_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_PAT", "ghp_test")
    a = GitHubModelsAgent()
    assert a.api_key == "ghp_test"


@pytest.mark.asyncio
async def test_github_models_happy_path(monkeypatch):
    monkeypatch.setenv("GITHUB_MODELS_ENABLED", "true")
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "ghp_test")
    a = GitHubModelsAgent()
    with patch("app.agents.github_models.httpx.AsyncClient",
               return_value=_fake_async_client(_mock_chat("from gh models"))):
        r = await a.respond("hi", 5000)
    assert r.success is True
    assert r.content == "from gh models"
