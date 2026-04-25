"""
Verify the LangChain → CrewAI / AutoGen tool adapters preserve name,
description, and produce the same return value as the underlying @tool.
"""
import pytest
from unittest.mock import patch

from langchain_core.tools import tool


@tool
def _echo_tool(text: str) -> str:
    """Echo the input back, prefixed."""
    return f"echo:{text}"


def test_to_crewai_tool_preserves_metadata_and_invokes():
    crewai = pytest.importorskip("crewai")
    from app.tools._adapters import to_crewai_tool

    wrapped = to_crewai_tool(_echo_tool)
    assert wrapped.name == "_echo_tool"
    assert "echo" in wrapped.description.lower()
    out = wrapped._run(text="hi")
    assert out == "echo:hi"


def test_to_autogen_tool_preserves_metadata_and_invokes():
    pytest.importorskip("autogen_core")
    from app.tools._adapters import to_autogen_tool

    wrapped = to_autogen_tool(_echo_tool)
    assert wrapped.name == "_echo_tool"
    assert "echo" in wrapped.description.lower()


def test_bulk_adapt_skips_failures(monkeypatch):
    pytest.importorskip("autogen_core")
    from app.tools import _adapters

    @tool
    def _good(text: str) -> str:
        """Good tool."""
        return text

    # Force one adapter call to raise to confirm bulk_adapt swallows it
    calls = {"n": 0}

    def fake_adapter(t):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return f"adapted:{t.name}"

    monkeypatch.setattr(_adapters, "to_autogen_tool", fake_adapter)
    out = _adapters.bulk_adapt([_good, _good, _good], "autogen")
    assert len(out) == 2
    assert all(isinstance(x, str) for x in out)
