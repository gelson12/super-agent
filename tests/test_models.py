"""
Unit tests for model adapters.
All LLM calls are mocked — no real API keys needed to run these.
"""
from unittest.mock import patch, MagicMock
import pytest


# ── Claude ───────────────────────────────────────────────────────────────────

def test_ask_claude_returns_text():
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = "Hello from Claude"

    mock_resp = MagicMock()
    mock_resp.content = [mock_block]

    with patch("app.models.claude._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        with patch("app.models.claude.settings") as mock_settings:
            mock_settings.anthropic_api_key = "test-key"
            mock_settings.max_tokens_claude = 100
            from app.models.claude import ask_claude
            result = ask_claude("Say hello")

    assert result == "Hello from Claude"


def test_ask_claude_no_key():
    with patch("app.models.claude.settings") as mock_settings:
        mock_settings.anthropic_api_key = ""
        from app.models.claude import ask_claude
        result = ask_claude("test")
    assert "ANTHROPIC_API_KEY not set" in result


# ── Gemini ───────────────────────────────────────────────────────────────────

def test_ask_gemini_returns_text():
    mock_resp = MagicMock()
    mock_resp.text = "Hello from Gemini"

    with patch("app.models.gemini._get_client") as mock_client:
        mock_client.return_value.models.generate_content.return_value = mock_resp
        with patch("app.models.gemini.settings") as mock_settings:
            mock_settings.gemini_api_key = "test-key"
            from app.models.gemini import ask_gemini
            result = ask_gemini("Say hello")

    assert result == "Hello from Gemini"


def test_ask_gemini_no_key():
    with patch("app.models.gemini.settings") as mock_settings:
        mock_settings.gemini_api_key = ""
        from app.models.gemini import ask_gemini
        result = ask_gemini("test")
    assert "GEMINI_API_KEY not set" in result


# ── DeepSeek ─────────────────────────────────────────────────────────────────

def test_ask_deepseek_returns_text():
    mock_choice = MagicMock()
    mock_choice.message.content = "Hello from DeepSeek"

    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]

    with patch("app.models.deepseek._get_client") as mock_client:
        mock_client.return_value.chat.completions.create.return_value = mock_resp
        with patch("app.models.deepseek.settings") as mock_settings:
            mock_settings.deepseek_api_key = "test-key"
            mock_settings.max_tokens_deepseek = 100
            from app.models.deepseek import ask_deepseek
            result = ask_deepseek("Say hello")

    assert result == "Hello from DeepSeek"


def test_ask_deepseek_no_key():
    with patch("app.models.deepseek.settings") as mock_settings:
        mock_settings.deepseek_api_key = ""
        from app.models.deepseek import ask_deepseek
        result = ask_deepseek("test")
    assert "DEEPSEEK_API_KEY not set" in result
