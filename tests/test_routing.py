"""
Unit tests for the semantic router and dispatcher.
All LLM calls are mocked.
"""
from unittest.mock import patch
import pytest

from app.routing.classifier import classify_request
from app.routing.dispatcher import dispatch


# ── Classifier ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gemini_reply,expected", [
    ("GEMINI", "GEMINI"),
    ("DEEPSEEK", "DEEPSEEK"),
    ("CLAUDE", "CLAUDE"),
    ("gemini", "GEMINI"),        # lowercase normalised
    ("claude.", "CLAUDE"),       # trailing punctuation stripped
    ("UNKNOWN", "GEMINI"),       # unrecognised → fallback
    ("", "GEMINI"),              # empty → fallback
])
def test_classifier_normalisation(gemini_reply, expected):
    with patch("app.routing.classifier.ask_gemini", return_value=gemini_reply):
        assert classify_request("any input") == expected


# ── Dispatcher ────────────────────────────────────────────────────────────────

def test_dispatch_routes_to_gemini():
    with patch("app.routing.dispatcher.classify_request", return_value="GEMINI"):
        with patch("app.routing.dispatcher.ask_gemini", return_value="Gemini reply") as mock_g:
            result = dispatch("classify this")
    assert result["model_used"] == "GEMINI"
    assert result["response"] == "Gemini reply"
    assert result["routed_by"] == "classifier"


def test_dispatch_routes_to_claude():
    with patch("app.routing.dispatcher.classify_request", return_value="CLAUDE"):
        with patch("app.routing.dispatcher.ask_claude", return_value="Claude reply"):
            result = dispatch("write an email")
    assert result["model_used"] == "CLAUDE"
    assert result["routed_by"] == "classifier"


def test_dispatch_force_model():
    with patch("app.routing.dispatcher.ask_deepseek", return_value="DS reply"):
        result = dispatch("code something", force_model="DEEPSEEK")
    assert result["model_used"] == "DEEPSEEK"
    assert result["routed_by"] == "forced"


def test_dispatch_invalid_force_model():
    result = dispatch("hello", force_model="OPENAI")
    assert result["model_used"] is None
    assert "Unknown model" in result["response"]
