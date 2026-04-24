from unittest.mock import AsyncMock, patch

import pytest

from app.agents.qwen import QwenAgent


async def _fake_proc(stdout: bytes, stderr: bytes, returncode: int):
    p = AsyncMock()
    p.returncode = returncode
    async def _comm(*_a, **_kw):
        return stdout, stderr
    p.communicate = _comm
    p.kill = lambda: None
    p.wait = AsyncMock(return_value=returncode)
    return p


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("QWEN_ENABLED", "true")
    return QwenAgent()


@pytest.mark.asyncio
async def test_qwen_disabled_by_default(monkeypatch):
    monkeypatch.setenv("QWEN_ENABLED", "false")
    r = await QwenAgent().respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "disabled"


@pytest.mark.asyncio
async def test_qwen_happy_path(agent):
    proc = await _fake_proc(b"qwen says hello\n", b"", 0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        r = await agent.respond("hi", 5000)
    assert r.success is True
    assert r.content == "qwen says hello"
    assert r.agent_id == "qwen"


@pytest.mark.asyncio
async def test_qwen_binary_missing(agent):
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError)):
        r = await agent.respond("hi", 5000)
    assert r.success is False
    assert r.error_class == "binary_not_found"
