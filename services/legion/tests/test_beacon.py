import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from app import beacon as beacon_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PRIMARY_BEACON_SECRET", "test-beacon-secret")
    # Reset state between tests
    beacon_mod._state.last_beacon_ts = 0.0
    beacon_mod._state.last_health_score = 0.0
    beacon_mod._state.consecutive_beacons = 0
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(beacon_mod.router)
    return TestClient(app)


def _sign(body: bytes, ts: int, secret: str) -> str:
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(str(ts).encode())
    mac.update(b"\n")
    mac.update(body)
    return mac.hexdigest()


def test_beacon_valid_hmac_accepted(client):
    body = b'{"health_score":0.9}'
    ts = int(time.time())
    sig = _sign(body, ts, "test-beacon-secret")
    r = client.post(
        "/legion/primary-beacon",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Beacon-Ts": str(ts),
            "X-Beacon-Sig": sig,
        },
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert beacon_mod._state.last_health_score == 0.9


def test_beacon_bad_sig_rejected(client):
    body = b'{"health_score":0.9}'
    ts = int(time.time())
    r = client.post(
        "/legion/primary-beacon",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Beacon-Ts": str(ts),
            "X-Beacon-Sig": "bad",
        },
    )
    assert r.status_code == 401


def test_primary_healthy_after_beacon(client):
    body = b'{"health_score":0.85}'
    ts = int(time.time())
    sig = _sign(body, ts, "test-beacon-secret")
    client.post(
        "/legion/primary-beacon",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Beacon-Ts": str(ts),
            "X-Beacon-Sig": sig,
        },
    )
    assert beacon_mod.primary_healthy() is True


def test_primary_not_healthy_when_no_beacons(client):
    assert beacon_mod.primary_healthy() is False
