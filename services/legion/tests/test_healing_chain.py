import pytest

from app.healing import chain as chain_module
from app.healing.chain import run_chain


@pytest.fixture(autouse=True)
def _patch_state(monkeypatch):
    async def noop(*a, **kw):
        return None
    monkeypatch.setattr(chain_module, "record_healing_attempt", noop)
    monkeypatch.setattr(chain_module, "set_role", noop)


@pytest.mark.asyncio
async def test_chain_stops_at_l1_when_l1_succeeds(monkeypatch):
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: True)
    called = {"l2": False, "l3": False, "l4": False}
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: called.__setitem__("l2", True) or False)
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: called.__setitem__("l3", True) or False)

    async def _l4():
        called["l4"] = True
        return False
    monkeypatch.setattr(chain_module, "login_via_playwright", _l4)

    layer = await run_chain("B")
    assert layer == "L1"
    assert called == {"l2": False, "l3": False, "l4": False}


@pytest.mark.asyncio
async def test_chain_falls_through_to_l2(monkeypatch):
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: True)
    called = {"l3": False, "l4": False}
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: called.__setitem__("l3", True) or False)

    async def _l4():
        called["l4"] = True
        return False
    monkeypatch.setattr(chain_module, "login_via_playwright", _l4)

    layer = await run_chain("B")
    assert layer == "L2"
    assert called == {"l3": False, "l4": False}


@pytest.mark.asyncio
async def test_chain_exhausts_when_all_layers_fail(monkeypatch):
    monkeypatch.setattr(chain_module, "restore_from_volume", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_env", lambda: False)
    monkeypatch.setattr(chain_module, "restore_from_refresh_token", lambda: False)

    async def _l4():
        return False
    monkeypatch.setattr(chain_module, "login_via_playwright", _l4)

    layer = await run_chain("B")
    assert layer is None
