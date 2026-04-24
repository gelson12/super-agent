import time

import pytest

from app import circuit
from app.circuit import CircuitState


@pytest.fixture(autouse=True)
async def _reset():
    await circuit.reset()
    yield
    await circuit.reset()


@pytest.mark.asyncio
async def test_fresh_agent_is_closed_and_allowed():
    assert await circuit.allow("fresh_agent") is True
    snap = await circuit.snapshot()
    assert snap["fresh_agent"]["state"] == CircuitState.CLOSED.value


@pytest.mark.asyncio
async def test_trips_open_after_threshold_failures(monkeypatch):
    # default threshold is 5 per legion_config.yaml
    for _ in range(5):
        await circuit.record_failure("bad_agent")
    snap = await circuit.snapshot()
    assert snap["bad_agent"]["state"] == CircuitState.OPEN.value
    assert await circuit.allow("bad_agent") is False


@pytest.mark.asyncio
async def test_success_resets_consecutive_errors():
    for _ in range(3):
        await circuit.record_failure("flaky_agent")
    await circuit.record_success("flaky_agent")
    snap = await circuit.snapshot()
    assert snap["flaky_agent"]["state"] == CircuitState.CLOSED.value
    assert snap["flaky_agent"]["consecutive_errors"] == 0


@pytest.mark.asyncio
async def test_open_transitions_to_half_open_after_cooldown(monkeypatch):
    for _ in range(5):
        await circuit.record_failure("temp_agent")
    # Force past cooldown by rewinding opened_at
    b = circuit._breakers["temp_agent"]
    b.opened_at = time.monotonic() - (b.cooldown_s + 1)
    assert await circuit.allow("temp_agent") is True
    snap = await circuit.snapshot()
    assert snap["temp_agent"]["state"] == CircuitState.HALF_OPEN.value


@pytest.mark.asyncio
async def test_half_open_admits_only_one_probe():
    for _ in range(5):
        await circuit.record_failure("probe_agent")
    b = circuit._breakers["probe_agent"]
    b.opened_at = time.monotonic() - (b.cooldown_s + 1)
    # First call -> HALF_OPEN and admits probe
    assert await circuit.allow("probe_agent") is True
    # Second call while still HALF_OPEN -> denied
    assert await circuit.allow("probe_agent") is False


@pytest.mark.asyncio
async def test_half_open_probe_failure_reopens_breaker():
    for _ in range(5):
        await circuit.record_failure("reopen_agent")
    b = circuit._breakers["reopen_agent"]
    b.opened_at = time.monotonic() - (b.cooldown_s + 1)
    await circuit.allow("reopen_agent")  # -> HALF_OPEN
    await circuit.record_failure("reopen_agent")
    snap = await circuit.snapshot()
    assert snap["reopen_agent"]["state"] == CircuitState.OPEN.value
