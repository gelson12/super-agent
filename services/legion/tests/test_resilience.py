"""
Legion resilience smoke tests.

Tests that each agent self-reports correctly on missing API keys,
429 cooldown state, and disabled status — without making real HTTP calls.
"""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import quota_state
from app.agents.cerebras import CerebrasAgent
from app.agents.chatgpt import ChatGPTAgent
from app.agents.deepseek import DeepSeekAgent
from app.agents.github_models import GitHubModelsAgent
from app.agents.groq import GroqAgent
from app.agents.mistral import MistralAgent
from app.agents.ollama import OllamaAgent
from app.agents.sambanova import SambaNovaAgent


# ── Helpers ──────────────────────────────────────────────────────────────────

def _enable(monkeypatch, agent_class, key_var: str, key_val: str = "test-key"):
    """Patch env so the agent believes it is enabled with a non-empty API key."""
    enabled_var = agent_class.agent_id.upper() + "_ENABLED"
    # Special cases
    if agent_class is ChatGPTAgent:
        enabled_var = "CHATGPT_ENABLED"
        key_var = "OPENAI_API_KEY"
    elif agent_class is GitHubModelsAgent:
        enabled_var = "GITHUB_MODELS_ENABLED"
        key_var = "GITHUB_MODELS_TOKEN"
    monkeypatch.setenv(enabled_var, "true")
    monkeypatch.setenv(key_var, key_val)
    return agent_class()


# ── Disabled / no-key tests ───────────────────────────────────────────────────

@pytest.mark.parametrize("agent_class,key_var", [
    (GroqAgent,        "GROQ_API_KEY"),
    (CerebrasAgent,    "CEREBRAS_API_KEY"),
    (SambaNovaAgent,   "SAMBANOVA_API_KEY"),
    (DeepSeekAgent,    "DEEPSEEK_API_KEY"),
    (MistralAgent,     "MISTRAL_API_KEY"),
    (ChatGPTAgent,     "OPENAI_API_KEY"),
    (GitHubModelsAgent,"GITHUB_MODELS_TOKEN"),
])
def test_disabled_returns_immediately(agent_class, key_var, monkeypatch):
    """Agent returns disabled error_class with zero latency when ENABLED=false."""
    monkeypatch.setenv(agent_class.agent_id.upper() + "_ENABLED" if agent_class is not ChatGPTAgent
                       else "CHATGPT_ENABLED", "false")
    monkeypatch.delenv(key_var, raising=False)
    agent = agent_class()
    resp = asyncio.get_event_loop().run_until_complete(agent.respond("hello", 5000))
    assert not resp.success
    assert resp.error_class in ("disabled", "no_api_key")
    assert resp.latency_ms == 0


@pytest.mark.parametrize("agent_class,enabled_var,key_var", [
    (GroqAgent,        "GROQ_ENABLED",         "GROQ_API_KEY"),
    (CerebrasAgent,    "CEREBRAS_ENABLED",      "CEREBRAS_API_KEY"),
    (SambaNovaAgent,   "SAMBANOVA_ENABLED",     "SAMBANOVA_API_KEY"),
    (DeepSeekAgent,    "DEEPSEEK_ENABLED",      "DEEPSEEK_API_KEY"),
    (MistralAgent,     "MISTRAL_ENABLED",       "MISTRAL_API_KEY"),
    (ChatGPTAgent,     "CHATGPT_ENABLED",       "OPENAI_API_KEY"),
    (GitHubModelsAgent,"GITHUB_MODELS_ENABLED", "GITHUB_MODELS_TOKEN"),
])
def test_no_key_returns_no_api_key(agent_class, enabled_var, key_var, monkeypatch):
    """Agent returns no_api_key when enabled=true but key is missing."""
    monkeypatch.setenv(enabled_var, "true")
    monkeypatch.delenv(key_var, raising=False)
    agent = agent_class()
    resp = asyncio.get_event_loop().run_until_complete(agent.respond("hello", 5000))
    assert not resp.success
    assert resp.error_class == "no_api_key"


# ── 429 cooldown tests ────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent_class,enabled_var,key_var,model_var,model_default", [
    (GroqAgent,        "GROQ_ENABLED",         "GROQ_API_KEY",         "GROQ_MODEL",         "llama-3.3-70b-versatile"),
    (CerebrasAgent,    "CEREBRAS_ENABLED",      "CEREBRAS_API_KEY",     "CEREBRAS_MODEL",     "llama3.1-8b"),
    (SambaNovaAgent,   "SAMBANOVA_ENABLED",     "SAMBANOVA_API_KEY",    "SAMBANOVA_MODEL",    "Meta-Llama-3.3-70B-Instruct"),
    (DeepSeekAgent,    "DEEPSEEK_ENABLED",      "DEEPSEEK_API_KEY",     "DEEPSEEK_MODEL",     "deepseek-reasoner"),
    (MistralAgent,     "MISTRAL_ENABLED",       "MISTRAL_API_KEY",      "MISTRAL_MODEL",      "codestral-latest"),
    (ChatGPTAgent,     "CHATGPT_ENABLED",       "OPENAI_API_KEY",       "OPENAI_MODEL",       "gpt-4o-mini"),
    (GitHubModelsAgent,"GITHUB_MODELS_ENABLED", "GITHUB_MODELS_TOKEN",  "GITHUB_MODELS_MODEL","openai/gpt-4o-mini"),
])
def test_rate_limit_cooldown_skips_http(
    agent_class, enabled_var, key_var, model_var, model_default, monkeypatch
):
    """Agent returns rate_limit_cooldown immediately when quota_state marks model exhausted."""
    monkeypatch.setenv(enabled_var, "true")
    monkeypatch.setenv(key_var, "test-key")
    model = os.environ.get(model_var, model_default)
    agent = agent_class()
    quota_state.mark_exhausted_for(agent.agent_id, agent.model, 300, reason="test")
    try:
        resp = asyncio.get_event_loop().run_until_complete(agent.respond("hello", 5000))
        assert not resp.success
        assert resp.error_class == "rate_limit_cooldown"
        assert resp.latency_ms == 0
    finally:
        # Clean up quota state so other tests aren't affected
        from app import quota_state as _qs
        import json, os as _os
        try:
            state_file = _qs._STATE_FILE
            if _os.path.exists(state_file):
                with open(state_file) as f:
                    data = json.load(f)
                key = f"{agent.agent_id}::{agent.model}"
                data.pop(key, None)
                with open(state_file, "w") as f:
                    json.dump(data, f)
        except Exception:
            pass


# ── Ollama auto-disable test ──────────────────────────────────────────────────

def test_ollama_auto_disables_on_connect_error(monkeypatch):
    """Ollama permanently disables itself when the host is unreachable."""
    import httpx
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")

    agent = OllamaAgent()
    assert agent.enabled is True

    async def _run():
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("refused")):
            return await agent.respond("hello", 3000)

    resp = asyncio.get_event_loop().run_until_complete(_run())
    assert not resp.success
    assert resp.error_class == "connect_error_auto_disabled"
    assert agent.enabled is False, "Ollama should self-disable after ConnectError"

    # Second call should return disabled immediately (no HTTP attempt)
    resp2 = asyncio.get_event_loop().run_until_complete(agent.respond("hello", 3000))
    assert resp2.error_class == "disabled"
    assert resp2.latency_ms == 0


# ── Successful response test (mocked HTTP) ───────────────────────────────────

def test_groq_success_path(monkeypatch):
    """Groq returns a successful AgentResponse when HTTP returns 200 with valid JSON."""
    monkeypatch.setenv("GROQ_ENABLED", "true")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    agent = GroqAgent()

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello from Groq!"}}],
        "usage": {"total_tokens": 10},
    }
    mock_response.raise_for_status = MagicMock()

    async def _run():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            return await agent.respond("Hello", 5000)

    resp = asyncio.get_event_loop().run_until_complete(_run())
    assert resp.success
    assert resp.content == "Hello from Groq!"
    assert resp.self_confidence == 0.6


# ── Refinement quota-awareness test ─────────────────────────────────────────

def test_refine_pick_agent_skips_exhausted(monkeypatch):
    """_pick_agent skips agents with active quota exhaustion."""
    from app.refine import _pick_agent

    class FakeAgent:
        def __init__(self, aid):
            self.agent_id = aid
            self.enabled = True
            self.model = aid + "-model"

    agents = {
        "groq": FakeAgent("groq"),
        "cerebras": FakeAgent("cerebras"),
    }
    quota_state.mark_exhausted_for("groq", "groq-model", 300, reason="test")
    try:
        picked = _pick_agent(["groq", "cerebras"], agents, exclude="")
        assert picked is not None
        assert picked.agent_id == "cerebras", "Should skip exhausted groq"
    finally:
        try:
            import json, os as _os
            from app import quota_state as _qs
            state_file = _qs._STATE_FILE
            if _os.path.exists(state_file):
                with open(state_file) as f:
                    data = json.load(f)
                data.pop("groq::groq-model", None)
                with open(state_file, "w") as f:
                    json.dump(data, f)
        except Exception:
            pass
