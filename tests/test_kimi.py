from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.kimi import KimiAgent


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("KIMI_ENABLED", "true")
    return KimiAgent()


async def _fake_proc(stdout: bytes, stderr: bytes, returncode: int, delay: float = 0.0):
    proc = AsyncMock()
    proc.returncode = returncode

    async def _communicate(*_a, **_kw):
        if delay:
            await asyncio.sleep(delay)
        return stdout, stderr

    proc.communicate = _communicate
    proc.kill = lambda: None
    proc.wait = AsyncMock(return_value=returncode)
    return proc


@pytest.mark.asyncio
async def test_kimi_happy_path(agent):
    proc = await _fake_proc(b"hello from kimi\n", b"", 0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        resp = await agent.respond("hi", 5000)
    assert resp.success is True
    assert resp.content == "hello from kimi"
    assert resp.agent_id == "kimi"
    assert resp.latency_ms >= 0


@pytest.mark.asyncio
async def test_kimi_nonzero_exit(agent):
    proc = await _fake_proc(b"", b"boom", 1)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        resp = await agent.respond("hi", 5000)
    assert resp.success is False
    assert resp.error_class == "exit_1"


@pytest.mark.asyncio
async def test_kimi_empty_output(agent):
    proc = await _fake_proc(b"   \n  ", b"", 0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        resp = await agent.respond("hi", 5000)
    assert resp.success is False
    assert resp.error_class == "empty_output"


@pytest.mark.asyncio
async def test_kimi_disabled_when_env_false(monkeypatch):
    monkeypatch.setenv("KIMI_ENABLED", "false")
    agent = KimiAgent()
    resp = await agent.respond("hi", 5000)
    assert resp.success is False
    assert resp.error_class == "disabled"


@pytest.mark.asyncio
async def test_kimi_timeout(agent):
    proc = await _fake_proc(b"slow", b"", 0, delay=1.0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        resp = await agent.respond("hi", 100)  # 100ms deadline
    assert resp.success is False
    assert resp.error_class == "subprocess_timeout"
