import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import verification_queue
from app.webhook import router as webhook_router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LEGION_WEBHOOK_SECRET", "test-webhook-secret")
    verification_queue.drain()
    app = FastAPI()
    app.include_router(webhook_router)
    return TestClient(app)


def test_webhook_rejects_missing_secret(client):
    r = client.post(
        "/webhook/verification-code",
        json={"url": "https://claude.ai/magic?t=x", "account": "B"},
    )
    assert r.status_code == 401


def test_webhook_rejects_wrong_secret(client):
    r = client.post(
        "/webhook/verification-code",
        json={"url": "https://claude.ai/magic?t=x"},
        headers={"X-Webhook-Secret": "nope", "X-Account-Target": "B"},
    )
    assert r.status_code == 401


def test_webhook_accepts_valid_secret_and_enqueues(client):
    r = client.post(
        "/webhook/verification-code",
        json={"url": "https://claude.ai/magic?t=x", "code": "123456", "account": "B"},
        headers={"X-Webhook-Secret": "test-webhook-secret", "X-Account-Target": "B"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    async def _consume():
        return await verification_queue.get(timeout_s=1)
    msg = asyncio.run(_consume())
    assert msg is not None
    assert msg.code == "123456"
    assert msg.account == "B"


def test_webhook_ignores_account_a_target(client):
    r = client.post(
        "/webhook/verification-code",
        json={"url": "x", "account": "A"},
        headers={"X-Webhook-Secret": "test-webhook-secret", "X-Account-Target": "A"},
    )
    assert r.status_code == 200
    assert r.json().get("ignored") is True

    # Nothing should have been enqueued
    async def _try_drain():
        return await verification_queue.get(timeout_s=0.2)
    msg = asyncio.run(_try_drain())
    assert msg is None


def test_webhook_503_when_secret_not_configured(monkeypatch):
    monkeypatch.delenv("LEGION_WEBHOOK_SECRET", raising=False)
    app = FastAPI()
    app.include_router(webhook_router)
    c = TestClient(app)
    r = c.post(
        "/webhook/verification-code",
        json={"url": "x"},
        headers={"X-Webhook-Secret": "anything"},
    )
    assert r.status_code == 503
