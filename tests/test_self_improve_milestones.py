"""
Tests for the self-improvement milestone (M1 + M2 of the audit).

Covers:
  - routing_advisor returns sane RouteHint under different budget tiers
  - tiered_agent_invoke recursion guard hard-fails above _MAX_AGENT_DEPTH
  - config.service_url() and list_services() resolve known names
  - /admin/tokens reports presence-only without leaking values
  - anomaly_alerter._AUTOMATION_REGISTRY wires the right handlers
  - insight_log writes both 'session' and 'session_id' fields (B1)
  - insight_log._normalize_model no longer collapses agent labels into CLAUDE (B3)
"""
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ── B1: insight_log writes both session keys ─────────────────────────────────

def test_insight_log_writes_both_session_and_session_id():
    from app.learning.insight_log import InsightLog
    log = InsightLog()
    # Bypass real I/O — monkey-patch the buffer flush to a no-op
    with patch.object(log, "_flush"):
        log.record("hello", "HAIKU", "world", "test_route", 1, session="abc-123")
    entry = log._buffer[-1]
    assert entry["session"] == "abc-123"
    assert entry["session_id"] == "abc-123"


# ── B3: SHELL/SELF_IMPROVE no longer normalised to CLAUDE ────────────────────

def test_normalize_model_keeps_agent_labels_distinct():
    from app.learning.insight_log import _normalize_model
    assert _normalize_model("SHELL") == "SHELL"
    assert _normalize_model("SELF_IMPROVE") == "SELF_IMPROVE"
    assert _normalize_model("GITHUB") == "GITHUB"
    assert _normalize_model("N8N") == "N8N"
    # True aliases still collapse
    assert _normalize_model("HAIKU") == "CLAUDE"
    assert _normalize_model("CLAUDE+SEARCH") == "CLAUDE"
    assert _normalize_model("GEMINI_CLI") == "GEMINI"


# ── G7: recursion guard ──────────────────────────────────────────────────────

def test_tiered_agent_invoke_recursion_guard():
    from app.agents import agent_routing as ar

    # Simulate being already deep in a recursion stack
    ar._invoke_depth.n = ar._MAX_AGENT_DEPTH  # next call exceeds limit
    try:
        out = ar.tiered_agent_invoke(
            message="anything",
            system_prompt="sys",
            tools=[],
            agent_type="default",
            source="recursion-test",
        )
        assert "recursion guard" in out
        # Counter must be reset after the guard returns
        assert ar._get_depth() == ar._MAX_AGENT_DEPTH
    finally:
        ar._invoke_depth.n = 0


# ── I2: service_url + list_services ──────────────────────────────────────────

def test_service_url_resolves_known_names():
    from app.config import service_url, list_services
    services = list_services()
    assert "cli_worker" in services
    assert "obsidian_vault" in services
    assert "n8n" in services
    assert "legion" in services
    # cli_worker falls back to inspiring_cat when CLI_WORKER_URL is unset
    assert service_url("cli_worker")
    # Unknown name returns empty string, not raise
    assert service_url("does_not_exist") == ""


# ── I4: /admin/tokens reports presence only ──────────────────────────────────

def test_admin_tokens_endpoint_no_secrets_leaked():
    resp = client.get("/admin/tokens")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "models" in data
    assert "infrastructure" in data
    assert "n8n" in data
    # Every entry should be {name, set, purpose} — no value field anywhere
    for group, items in data.items():
        if group == "resolved_services":
            continue
        for item in items:
            assert set(item.keys()) == {"name", "set", "purpose"}
            assert isinstance(item["set"], bool)


# ── G1 / G5 / G8: routing_advisor under different budget tiers ───────────────

def test_advisor_full_budget_has_no_deprio():
    from app.routing import routing_advisor
    with patch.object(routing_advisor, "_read_budget_tier", return_value="full"), \
         patch.object(routing_advisor, "_read_strikes", return_value=set()), \
         patch.object(routing_advisor, "_read_best_model", return_value=None), \
         patch.object(routing_advisor, "_algorithm_run", return_value=None):
        h = routing_advisor.recommend("any task", classification="shell")
    assert h.budget_tier == "full"
    assert h.deprioritize == []


def test_advisor_critical_budget_deprioritizes_claude():
    from app.routing import routing_advisor
    with patch.object(routing_advisor, "_read_budget_tier", return_value="critical"), \
         patch.object(routing_advisor, "_read_strikes", return_value=set()), \
         patch.object(routing_advisor, "_read_best_model", return_value=None), \
         patch.object(routing_advisor, "_algorithm_run", return_value=None):
        h = routing_advisor.recommend("any", classification="shell")
    assert h.preferred_model == "DEEPSEEK"
    assert "CLAUDE" in h.deprioritize


def test_advisor_uses_wisdom_when_budget_full():
    from app.routing import routing_advisor
    with patch.object(routing_advisor, "_read_budget_tier", return_value="full"), \
         patch.object(routing_advisor, "_read_strikes", return_value=set()), \
         patch.object(routing_advisor, "_read_best_model", return_value="GEMINI"), \
         patch.object(routing_advisor, "_algorithm_run", return_value=None):
        h = routing_advisor.recommend("explain X", classification="research")
    assert h.preferred_model == "GEMINI"
    assert any("wisdom_store" in r for r in h.reasons)


def test_advisor_strike_deprioritizes_matching_model():
    from app.routing import routing_advisor
    with patch.object(routing_advisor, "_read_budget_tier", return_value="full"), \
         patch.object(routing_advisor, "_read_strikes", return_value={"DeepSeek worker"}), \
         patch.object(routing_advisor, "_read_best_model", return_value=None), \
         patch.object(routing_advisor, "_algorithm_run", return_value=None):
        h = routing_advisor.recommend("anything", classification="shell")
    assert "DEEPSEEK" in h.deprioritize


# ── G6: anomaly_alerter automation registry ──────────────────────────────────

def test_anomaly_automation_registry_has_known_handlers():
    from app.learning import anomaly_alerter as aa
    assert "n8n_failures" in aa._AUTOMATION_REGISTRY
    assert "error_rate_spike" in aa._AUTOMATION_REGISTRY
    assert "disk_high" in aa._AUTOMATION_REGISTRY
    # cost_near_budget intentionally not auto-handled
    assert "cost_near_budget" not in aa._AUTOMATION_REGISTRY


def test_anomaly_automation_handlers_never_raise():
    from app.learning import anomaly_alerter as aa
    for name in aa._AUTOMATION_REGISTRY:
        # Even if downstream services are absent, handlers must return a string.
        out = aa._run_automation(name)
        assert isinstance(out, str)
