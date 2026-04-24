import pytest

from app import verification_queue
from app.verification_queue import VerificationMessage, drain, get, put


@pytest.fixture(autouse=True)
def _clean():
    drain()
    yield
    drain()


@pytest.mark.asyncio
async def test_put_and_get_roundtrip():
    await put(VerificationMessage(url="u", code="1", account="B"))
    msg = await get(timeout_s=1)
    assert msg is not None
    assert msg.url == "u"
    assert msg.code == "1"
    assert msg.account == "B"


@pytest.mark.asyncio
async def test_get_returns_none_on_timeout():
    msg = await get(timeout_s=0.1)
    assert msg is None


@pytest.mark.asyncio
async def test_drain_removes_pending():
    await put(VerificationMessage(url="a", code=None, account="B"))
    await put(VerificationMessage(url="b", code=None, account="B"))
    n = drain()
    assert n == 2
    msg = await get(timeout_s=0.1)
    assert msg is None
