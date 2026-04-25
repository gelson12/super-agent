"""
Test for /chat/crew — patches CrewAI Crew.kickoff to return a canned result.
"""
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_chat_crew_returns_response():
    pytest.importorskip("crewai")

    fake_result = MagicMock()
    fake_result.raw = "crew final deliverable"

    with patch("app.agents.crewai_crew._build_crew") as mock_build, \
         patch("app.main.append_exchange"):
        mock_crew = MagicMock()
        mock_crew.kickoff.return_value = fake_result
        mock_build.return_value = mock_crew

        # Bypass the api-key check so the early-return doesn't fire
        with patch("app.agents.crewai_crew.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-test"
            mock_settings.crewai_process = "hierarchical"
            resp = client.post(
                "/chat/crew",
                json={"message": "build something", "session_id": "crew-1"},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["framework_used"] == "crewai"
    assert data["response"] == "crew final deliverable"


def test_chat_crew_handles_kickoff_error():
    pytest.importorskip("crewai")
    with patch("app.agents.crewai_crew._build_crew") as mock_build:
        mock_crew = MagicMock()
        mock_crew.kickoff.side_effect = RuntimeError("model down")
        mock_build.return_value = mock_crew

        with patch("app.agents.crewai_crew.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-test"
            mock_settings.crewai_process = "hierarchical"
            resp = client.post(
                "/chat/crew",
                json={"message": "x", "session_id": "crew-2"},
            )

    assert resp.status_code == 200
    assert "[crewai error" in resp.json()["response"]


def test_chat_crew_disabled(monkeypatch):
    pytest.importorskip("crewai")
    from app import config as _cfg
    monkeypatch.setattr(_cfg.settings, "frameworks_enabled", False)
    resp = client.post("/chat/crew", json={"message": "x"})
    assert resp.status_code == 200
    assert "disabled" in resp.json()["response"].lower()
