"""
Integration tests for FastAPI endpoints.
LLM calls are mocked — tests run without real API keys.
"""
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ── /health ───────────────────────────────────────────────────────────────────

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── /chat ─────────────────────────────────────────────────────────────────────

def test_chat_returns_response():
    with patch("app.main.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {
            "model_used": "GEMINI",
            "response": "Test response",
            "routed_by": "classifier",
        }
        with patch("app.main.append_exchange"):
            resp = client.post("/chat", json={"message": "Hello", "session_id": "test"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["response"] == "Test response"
    assert data["model_used"] == "GEMINI"
    assert data["session_id"] == "test"


def test_chat_empty_message():
    resp = client.post("/chat", json={"message": "", "session_id": "test"})
    assert resp.status_code == 422  # Pydantic min_length validation


def test_chat_missing_message():
    resp = client.post("/chat", json={"session_id": "test"})
    assert resp.status_code == 422


# ── /chat/direct ──────────────────────────────────────────────────────────────

def test_chat_direct_valid_model():
    with patch("app.main.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {
            "model_used": "CLAUDE",
            "response": "Claude direct reply",
            "routed_by": "forced",
        }
        with patch("app.main.append_exchange"):
            resp = client.post(
                "/chat/direct",
                json={"message": "write an email", "model": "CLAUDE", "session_id": "s1"},
            )

    assert resp.status_code == 200
    assert resp.json()["model_used"] == "CLAUDE"


def test_chat_direct_invalid_model():
    with patch("app.main.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {
            "model_used": None,
            "response": "Unknown model 'OPENAI'.",
            "routed_by": "forced",
        }
        resp = client.post(
            "/chat/direct",
            json={"message": "hello", "model": "OPENAI", "session_id": "s1"},
        )

    assert resp.status_code == 400


# ── /history ──────────────────────────────────────────────────────────────────

def test_get_history_empty():
    with patch("app.main.get_messages", return_value=[]):
        resp = client.get("/history/no-such-session")
    assert resp.status_code == 200
    assert resp.json() == []


def test_delete_history():
    with patch("app.main.clear_session"):
        resp = client.delete("/history/test-session")
    assert resp.status_code == 200
    assert resp.json()["cleared"] is True
