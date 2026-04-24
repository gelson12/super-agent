import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import auth
from app.auth import require_hmac, sign
from app.config import settings


@pytest.fixture(autouse=True)
def _secret():
    prev = settings.LEGION_API_SHARED_SECRET
    settings.LEGION_API_SHARED_SECRET = "test-shared-secret"
    yield
    settings.LEGION_API_SHARED_SECRET = prev


@pytest.fixture
def client():
    app = FastAPI()

    @app.post("/echo")
    async def echo(body: dict, _: None = __import__("fastapi").Depends(require_hmac)):
        return body

    return TestClient(app)


def test_valid_hmac_accepted(client):
    body = b'{"hello":"world"}'
    ts, sig = sign("test-shared-secret", body)
    r = client.post("/echo", content=body, headers={
        "Content-Type": "application/json",
        "X-Legion-Ts": ts,
        "X-Legion-Sig": sig,
    })
    assert r.status_code == 200
    assert r.json() == {"hello": "world"}


def test_bad_sig_rejected(client):
    body = b'{"hello":"world"}'
    ts, _ = sign("test-shared-secret", body)
    r = client.post("/echo", content=body, headers={
        "Content-Type": "application/json",
        "X-Legion-Ts": ts,
        "X-Legion-Sig": "deadbeef",
    })
    assert r.status_code == 401


def test_ts_skew_rejected(client):
    body = b'{"x":1}'
    old_ts = int(time.time()) - (auth.MAX_SKEW_S + 5)
    ts_str, sig = sign("test-shared-secret", body, ts=old_ts)
    r = client.post("/echo", content=body, headers={
        "Content-Type": "application/json",
        "X-Legion-Ts": ts_str,
        "X-Legion-Sig": sig,
    })
    assert r.status_code == 401


def test_missing_secret_returns_503(client):
    settings.LEGION_API_SHARED_SECRET = None
    body = b'{}'
    ts, sig = sign("irrelevant", body)
    r = client.post("/echo", content=body, headers={
        "Content-Type": "application/json",
        "X-Legion-Ts": ts,
        "X-Legion-Sig": sig,
    })
    assert r.status_code == 503
