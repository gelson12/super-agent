"""
AutoGen v0.4 multi-agent team — Architect / Implementer / Reviewer.

Async-first via autogen-agentchat + autogen-ext. Reuses TOOLSETS["engineering"]
through the to_autogen_tool adapter. The team terminates when the Reviewer says
"APPROVED" or after autogen_max_turns messages, whichever comes first.

Public entry: run_team(message, session_id) → dict
"""
from __future__ import annotations

from typing import Any
from ..config import settings
from ..tools.registry import get_toolset
from ..tools._adapters import bulk_adapt


_ARCHITECT_SYSTEM = (
    "You are the Architect. Read the user's task, then write a numbered, concrete plan "
    "the Implementer can execute. Keep plans short (≤6 steps). Do not call tools yourself."
)
_IMPLEMENTER_SYSTEM = (
    "You are the Implementer. Execute the Architect's plan using the provided tools. "
    "Report concrete results — file paths, IDs, status codes, exact tool outputs."
)
_REVIEWER_SYSTEM = (
    "You are the Reviewer. Inspect the Implementer's work against the Architect's plan. "
    "If the work is complete and correct, reply with the single word APPROVED. "
    "Otherwise reply with REVISE: <specific issue> on one line."
)


def _build_model_client():
    """Anthropic chat completion client for Sonnet."""
    from autogen_ext.models.anthropic import AnthropicChatCompletionClient
    return AnthropicChatCompletionClient(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
    )


def _build_haiku_client():
    from autogen_ext.models.anthropic import AnthropicChatCompletionClient
    return AnthropicChatCompletionClient(
        model="claude-haiku-4-5-20251001",
        api_key=settings.anthropic_api_key,
    )


def _build_team():
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.teams import SelectorGroupChat
    from autogen_agentchat.conditions import TextMentionTermination, MaxMessageTermination

    sonnet = _build_model_client()
    haiku = _build_haiku_client()

    eng_tools = bulk_adapt(get_toolset("engineering"), "autogen")

    architect = AssistantAgent(
        name="architect",
        model_client=sonnet,
        system_message=_ARCHITECT_SYSTEM,
    )
    implementer = AssistantAgent(
        name="implementer",
        model_client=sonnet,
        system_message=_IMPLEMENTER_SYSTEM,
        tools=eng_tools,
        reflect_on_tool_use=True,
    )
    reviewer = AssistantAgent(
        name="reviewer",
        model_client=haiku,
        system_message=_REVIEWER_SYSTEM,
    )

    termination = TextMentionTermination("APPROVED") | MaxMessageTermination(settings.autogen_max_turns)

    return SelectorGroupChat(
        participants=[architect, implementer, reviewer],
        model_client=haiku,
        termination_condition=termination,
        allow_repeated_speaker=False,
    )


def _format_transcript(result) -> str:
    """Pull the final assistant text out of a TaskResult."""
    msgs = getattr(result, "messages", None) or []
    for msg in reversed(msgs):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return "[autogen team: no response]"


async def run_team(message: str, session_id: str = "default") -> dict[str, Any]:
    """Run the Architect/Implementer/Reviewer team on a task."""
    if not settings.anthropic_api_key:
        return {
            "response": "[autogen team error: ANTHROPIC_API_KEY not set]",
            "framework_used": "autogen",
            "session_id": session_id,
            "turns": 0,
        }
    try:
        team = _build_team()
        result = await team.run(task=message)
    except Exception as e:
        return {
            "response": f"[autogen team error: {str(e)[:300]}]",
            "framework_used": "autogen",
            "session_id": session_id,
            "turns": 0,
        }

    return {
        "response": _format_transcript(result),
        "framework_used": "autogen",
        "session_id": session_id,
        "turns": len(getattr(result, "messages", []) or []),
    }
