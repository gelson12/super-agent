"""
Tool adapters — wrap LangChain @tool functions for CrewAI and AutoGen.

The @tool functions in app/tools/*.py are the canonical source. These adapters
provide thin wrappers so CrewAI agents and AutoGen AssistantAgents can call the
exact same code paths without duplicating tool definitions.

CrewAI and AutoGen are imported lazily so this module can be imported even if
those packages are not installed (useful for tests and dev environments).
"""
from __future__ import annotations

from typing import Any, Callable, Iterable
from langchain_core.tools import BaseTool


def _invoke_lc(lc_tool: BaseTool, payload: Any) -> str:
    """Invoke a LangChain tool with either a dict or a single positional value."""
    try:
        if isinstance(payload, dict):
            return str(lc_tool.invoke(payload))
        return str(lc_tool.invoke(payload))
    except Exception as e:
        return f"[tool {lc_tool.name} error: {str(e)[:200]}]"


# ── CrewAI ────────────────────────────────────────────────────────────────────

def to_crewai_tool(lc_tool: BaseTool):
    """Wrap a LangChain BaseTool as a CrewAI BaseTool."""
    from crewai.tools import BaseTool as CrewBaseTool  # lazy import
    from pydantic import BaseModel

    args_schema = getattr(lc_tool, "args_schema", None)
    if args_schema is None or not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
        # CrewAI requires a pydantic args_schema; synthesize a permissive one
        from pydantic import create_model
        args_schema = create_model(
            f"{lc_tool.name}_Args",
            __base__=BaseModel,
            input=(str, ...),
        )

    tool_name = lc_tool.name
    tool_description = lc_tool.description or f"Tool: {tool_name}"
    captured = lc_tool

    def _run_impl(self, **kwargs: Any) -> str:
        payload = kwargs if len(kwargs) != 1 else next(iter(kwargs.values()))
        return _invoke_lc(captured, payload)

    _Wrapped = type(
        f"CrewWrapped_{tool_name}",
        (CrewBaseTool,),
        {
            "__module__": __name__,
            "__qualname__": f"CrewWrapped_{tool_name}",
            "__annotations__": {
                "name": str,
                "description": str,
                "args_schema": type[BaseModel],
            },
            "name": tool_name,
            "description": tool_description,
            "args_schema": args_schema,
            "_run": _run_impl,
        },
    )
    return _Wrapped()


# ── AutoGen v0.4 (autogen-core FunctionTool) ──────────────────────────────────

def to_autogen_tool(lc_tool: BaseTool):
    """Wrap a LangChain BaseTool as an autogen-core FunctionTool."""
    from autogen_core.tools import FunctionTool  # lazy import

    name = lc_tool.name
    description = lc_tool.description or f"Tool: {name}"

    def _fn(input: str) -> str:
        return _invoke_lc(lc_tool, input)

    _fn.__name__ = name
    _fn.__doc__ = description

    return FunctionTool(_fn, description=description, name=name)


# ── Bulk ──────────────────────────────────────────────────────────────────────

def bulk_adapt(lc_tools: Iterable[BaseTool], target: str) -> list:
    """Adapt a list of LangChain tools to one of {'crewai', 'autogen'}."""
    target = target.lower()
    if target == "crewai":
        adapter: Callable = to_crewai_tool
    elif target == "autogen":
        adapter = to_autogen_tool
    else:
        raise ValueError(f"unknown adapter target {target!r}; use 'crewai' or 'autogen'")
    out = []
    for t in lc_tools:
        try:
            out.append(adapter(t))
        except Exception as e:
            # Don't let one bad tool kill the whole bundle
            from ..activity_log import bg_log
            try:
                bg_log(f"adapter skip {getattr(t, 'name', '?')} → {target}: {e}", source="tool_adapters")
            except Exception:
                pass
    return out
