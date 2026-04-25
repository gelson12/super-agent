"""
End-to-end test for the /chat/graph LangGraph endpoint.

We patch the heavy nodes (compete_and_plan, tiered_agent_invoke, ask_haiku)
so the graph runs deterministically without real LLM calls.
"""
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _patch_graph_nodes():
    """Stub every LLM-bound node in the StateGraph."""
    return [
        patch(
            "app.agents.graphs.plan_execute_critique.compete_and_plan",
            return_value="GOAL: stub plan",
        ),
        patch(
            "app.agents.graphs.plan_execute_critique.tiered_agent_invoke",
            return_value="stub execution result",
        ),
        patch(
            "app.learning.internal_llm.ask_internal_fast",
            return_value="VERDICT: APPROVED\nNOTES: looks good",
        ),
    ]


def test_chat_graph_happy_path():
    pytest.importorskip("langgraph")
    patches = _patch_graph_nodes()
    for p in patches:
        p.start()
    try:
        with patch("app.main.append_exchange"):
            resp = client.post(
                "/chat/graph",
                json={"message": "explain langgraph", "session_id": "graph-test-1"},
            )
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["framework_used"] == "langgraph"
    assert "stub execution result" in data["response"]
    assert data["extra"]["thread_id"]  # checkpointer wired


def test_chat_graph_retries_on_critique():
    pytest.importorskip("langgraph")
    plan_patch = patch(
        "app.agents.graphs.plan_execute_critique.compete_and_plan",
        return_value="plan",
    )
    exec_patch = patch(
        "app.agents.graphs.plan_execute_critique.tiered_agent_invoke",
        return_value="result",
    )
    # First critique → RETRY, second → APPROVED. Third would also be APPROVED
    # but max retries caps it.
    critique_responses = iter([
        "VERDICT: RETRY\nNOTES: needs more detail",
        "VERDICT: APPROVED\nNOTES: ok",
        "VERDICT: APPROVED\nNOTES: ok",
    ])
    crit_patch = patch(
        "app.learning.internal_llm.ask_internal_fast",
        side_effect=lambda *_a, **_k: next(critique_responses),
    )

    plan_patch.start(); exec_patch.start(); crit_patch.start()
    try:
        with patch("app.main.append_exchange"):
            resp = client.post(
                "/chat/graph",
                json={"message": "task", "session_id": "graph-test-2"},
            )
    finally:
        plan_patch.stop(); exec_patch.stop(); crit_patch.stop()

    assert resp.status_code == 200
    data = resp.json()
    assert data["extra"]["retries"] >= 1


def test_chat_graph_disabled_kill_switch(monkeypatch):
    pytest.importorskip("langgraph")
    from app import config as _cfg
    monkeypatch.setattr(_cfg.settings, "frameworks_enabled", False)
    resp = client.post("/chat/graph", json={"message": "x"})
    assert resp.status_code == 200
    assert "disabled" in resp.json()["response"].lower()
