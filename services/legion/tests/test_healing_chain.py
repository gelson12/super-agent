import pytest

from app.healing import chain as chain_module
from app.healing.chain import run_chain
from app.healing.l4_playwright import FailureSignature, L4Result


@pytest.fixture(autouse=True)
def _patch_state(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(chain_module, "record_healing_attempt", _noop)
    monkeypatch.setattr(chain_module, "set_role", _noop)


def _l4_result_stub(success: bool, signature: str | None = None):
    async def _inner(*a, **kw):
        return L4Result(success=success, failure_signature=signature, diag=None)
    return _inner


@pytest.mark.asyncio
async def test_chain_stops_at_l1(monkeypatch):
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: True)
    called = {"l2": False, "l3": False}
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: called.__setitem__("l2", True) or False)
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: called.__setitem__("l3", True) or False)
    monkeypatch.setattr(chain_module, "login_via_playwright", _l4_result_stub(False))
    monkeypatch.setattr(chain_module, "login_via_cdp", _l4_result_stub(False))
    assert await run_chain("B") == "L1"
    assert called == {"l2": False, "l3": False}


@pytest.mark.asyncio
async def test_chain_falls_through_to_l2(monkeypatch):
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: True)
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: False)
    monkeypatch.setattr(chain_module, "login_via_playwright", _l4_result_stub(False))
    monkeypatch.setattr(chain_module, "login_via_cdp", _l4_result_stub(False))
    assert await run_chain("B") == "L2"


@pytest.mark.asyncio
async def test_chain_runs_l5_when_l5_enabled_and_l4_recoverable(monkeypatch):
    monkeypatch.setenv("L5_ENABLED", "true")
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: False)
    monkeypatch.setattr(
        chain_module, "login_via_playwright",
        _l4_result_stub(False, FailureSignature.TIMEOUT),
    )
    monkeypatch.setattr(chain_module, "login_via_cdp", _l4_result_stub(True))
    assert await run_chain("B") == "L5"


@pytest.mark.asyncio
async def test_chain_skips_l5_on_terminal_lockout(monkeypatch):
    monkeypatch.setenv("L5_ENABLED", "true")
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: False)
    monkeypatch.setattr(
        chain_module, "login_via_playwright",
        _l4_result_stub(False, FailureSignature.LOCKED),
    )
    l5_was_called = {"yes": False}
    async def _l5(*a, **kw):
        l5_was_called["yes"] = True
        return L4Result(False)
    monkeypatch.setattr(chain_module, "login_via_cdp", _l5)
    assert await run_chain("B") is None
    assert l5_was_called["yes"] is False


@pytest.mark.asyncio
async def test_chain_skips_l5_when_flag_off(monkeypatch):
    monkeypatch.setenv("L5_ENABLED", "false")
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: False)
    monkeypatch.setattr(
        chain_module, "login_via_playwright",
        _l4_result_stub(False, FailureSignature.TIMEOUT),
    )
    l5_was_called = {"yes": False}
    async def _l5(*a, **kw):
        l5_was_called["yes"] = True
        return L4Result(False)
    monkeypatch.setattr(chain_module, "login_via_cdp", _l5)
    assert await run_chain("B") is None
    assert l5_was_called["yes"] is False
