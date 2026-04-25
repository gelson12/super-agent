"""
Test for /chat/groupchat — patches the AutoGen team.run() coroutine.
"""
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_chat_groupchat_terminates_on_approved():
    pytest.importorskip("autogen_agentchat")

    fake_msg = MagicMock()
    fake_msg.content = "APPROVED"
    fake_result = MagicMock()
    fake_result.messages = [
        MagicMock(content="plan: do X"),
        MagicMock(content="implementer ran tools"),
        fake_msg,
    ]

    fake_team = MagicMock()
    fake_team.run = AsyncMock(return_value=fake_result)

    with patch("app.agents.autogen_team._build_team", return_value=fake_team), \
         patch("app.main.append_exchange"):
        with patch("app.agents.autogen_team.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-test"
            mock_settings.autogen_max_turns = 12
            resp = client.post(
                "/chat/groupchat",
                json={"message": "design retry policy", "session_id": "team-1"},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["framework_used"] == "autogen"
    assert data["response"] == "APPROVED"
    assert data["extra"]["turns"] == 3


def test_chat_groupchat_handles_run_error():
    pytest.importorskip("autogen_agentchat")
    fake_team = MagicMock()
    fake_team.run = AsyncMock(side_effect=RuntimeError("provider 503"))
    with patch("app.agents.autogen_team._build_team", return_value=fake_team):
        with patch("app.agents.autogen_team.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-test"
            mock_settings.autogen_max_turns = 12
            resp = client.post(
                "/chat/groupchat",
                json={"message": "x", "session_id": "team-2"},
            )

    assert resp.status_code == 200
    assert "[autogen team error" in resp.json()["response"]


def test_chat_groupchat_disabled(monkeypatch):
    pytest.importorskip("autogen_agentchat")
    from app import config as _cfg
    monkeypatch.setattr(_cfg.settings, "frameworks_enabled", False)
    resp = client.post("/chat/groupchat", json={"message": "x"})
    assert resp.status_code == 200
    assert "disabled" in resp.json()["response"].lower()
