from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.groq import GroqAgent


def _mock_response(content: str, tokens: int = 100):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": tokens},
    })
    return r


@pytest.mark.asyncio
async def test_groq_disabled_when_flag_off(monkeypatch):
    monkeypatch.setenv("GROQ_ENABLED", "false")
    r = await GroqAgent().respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "disabled"


@pytest.mark.asyncio
async def test_groq_no_api_key(monkeypatch):
    monkeypatch.setenv("GROQ_ENABLED", "true")
    monkeypatch.setenv("GROQ_API_KEY", "")
    r = await GroqAgent().respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "no_api_key"


@pytest.mark.asyncio
async def test_groq_happy_path(monkeypatch):
    monkeypatch.setenv("GROQ_ENABLED", "true")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    a = GroqAgent()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_mock_response("hi from groq", tokens=42))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.agents.groq.httpx.AsyncClient", return_value=fake_client):
        r = await a.respond("hi", 5000)
    assert r.success is True
    assert r.content == "hi from groq"
    assert r.cost_cents == 0.0  # free tier
