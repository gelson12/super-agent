from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import memory_client


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_base_url_unset(monkeypatch):
    monkeypatch.delenv("SUPER_AGENT_BASE_URL", raising=False)
    assert await memory_client.fetch_relevant("hello") == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_for_short_query(monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_BASE_URL", "http://example.com")
    assert await memory_client.fetch_relevant("h") == []


@pytest.mark.asyncio
async def test_fetch_handles_http_error_gracefully(monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_BASE_URL", "http://example.com")
    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=Exception("boom"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.memory_client.httpx.AsyncClient", return_value=fake_client):
        out = await memory_client.fetch_relevant("hello world")
    assert out == []  # never raises, returns empty


@pytest.mark.asyncio
async def test_fetch_parses_results(monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_BASE_URL", "http://example.com")
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={
        "query": "hello",
        "count": 2,
        "results": [{"text": "one"}, {"text": "two"}],
    })
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.memory_client.httpx.AsyncClient", return_value=fake_client):
        out = await memory_client.fetch_relevant("hello world")
    assert len(out) == 2
    assert out[0]["text"] == "one"


def test_format_context_empty_input():
    assert memory_client.format_context_block([]) == ""


def test_format_context_truncates_at_max_chars():
    big = [{"text": "x" * 1000} for _ in range(10)]
    out = memory_client.format_context_block(big, max_chars=500)
    assert len(out) <= 800  # rough upper bound including framing


def test_format_context_renders_results():
    results = [{"text": "first memory"}, {"content": "second memory"}]
    out = memory_client.format_context_block(results)
    assert "first memory" in out
    assert "second memory" in out
    assert "[shared-memory context" in out


@pytest.mark.asyncio
async def test_augment_query_passthrough_when_no_memory(monkeypatch):
    monkeypatch.delenv("SUPER_AGENT_BASE_URL", raising=False)
    assert await memory_client.augment_query("plain question") == "plain question"
