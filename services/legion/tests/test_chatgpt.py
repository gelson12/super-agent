from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.chatgpt import ChatGPTAgent


def _mock_response(content: str, tokens: int = 100):
    """Build a mock httpx.Response-like object."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": tokens},
    })
    return resp


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("CHATGPT_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return ChatGPTAgent()


@pytest.mark.asyncio
async def test_chatgpt_disabled_when_flag_off(monkeypatch):
    monkeypatch.setenv("CHATGPT_ENABLED", "false")
    a = ChatGPTAgent()
    r = await a.respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "disabled"


@pytest.mark.asyncio
async def test_chatgpt_no_api_key(monkeypatch):
    monkeypatch.setenv("CHATGPT_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    a = ChatGPTAgent()
    r = await a.respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "no_api_key"


@pytest.mark.asyncio
async def test_chatgpt_happy_path(agent):
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_mock_response("hello back", tokens=50))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.agents.chatgpt.httpx.AsyncClient", return_value=fake_client):
        r = await agent.respond("hi", 5000)
    assert r.success is True
    assert r.content == "hello back"
    assert r.cost_cents > 0


@pytest.mark.asyncio
async def test_chatgpt_malformed_response(agent):
    bad = MagicMock()
    bad.raise_for_status = MagicMock()
    bad.json = MagicMock(return_value={})  # missing choices
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=bad)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.agents.chatgpt.httpx.AsyncClient", return_value=fake_client):
        r = await agent.respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "malformed_response"
