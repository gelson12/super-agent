"""
CrewAI hierarchical crew — Researcher / Engineer / QA.

Three role-based agents coordinated by a manager LLM.
- Researcher: read-only tools (search, github read, vault, db read)
- Engineer:   write tools (github write, shell, flutter)
- QA:         no tools, verifies the Engineer's deliverable

CrewAI's kickoff is synchronous, so run_crew wraps it in asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..config import settings
from ..tools.registry import get_toolset
from ..tools._adapters import bulk_adapt


def _llm(model: str):
    """Build a CrewAI LLM bound to Anthropic."""
    from crewai import LLM  # lazy import
    return LLM(
        model=f"anthropic/{model}",
        api_key=settings.anthropic_api_key,
    )


def _build_crew(message: str):
    from crewai import Agent, Task, Crew, Process

    sonnet = _llm("claude-sonnet-4-6")
    haiku = _llm("claude-haiku-4-5-20251001")

    research_tools = bulk_adapt(get_toolset("research"), "crewai")
    eng_tools = bulk_adapt(get_toolset("engineering"), "crewai")

    researcher = Agent(
        role="Researcher",
        goal="Gather authoritative context, prior art, and constraints for the task.",
        backstory="You read repositories, vault notes, search results, and DB metrics to brief the engineer.",
        tools=research_tools,
        llm=sonnet,
        allow_delegation=False,
        verbose=False,
    )
    engineer = Agent(
        role="Engineer",
        goal="Produce the concrete deliverable the user asked for.",
        backstory="You execute file edits, builds, and integration changes using the available tools.",
        tools=eng_tools,
        llm=sonnet,
        allow_delegation=True,
        verbose=False,
    )
    qa = Agent(
        role="QA",
        goal="Verify the engineer's output meets the request before returning to the user.",
        backstory="You do not execute changes — you read the deliverable and approve or request a fix.",
        tools=[],
        llm=haiku,
        allow_delegation=False,
        verbose=False,
    )

    research_task = Task(
        description=(
            f"Research everything needed for this user request:\n\n{message}\n\n"
            "Return: relevant files, prior decisions, constraints, and any blocking unknowns."
        ),
        agent=researcher,
        expected_output="A short research brief — bullet points, no prose.",
    )
    build_task = Task(
        description=(
            "Using the research brief, execute the user request end to end. "
            "Use tools — do not describe what you would do."
        ),
        agent=engineer,
        expected_output="Concrete deliverable with file paths, IDs, or status codes.",
        context=[research_task],
    )
    qa_task = Task(
        description=(
            "Inspect the engineer's deliverable. If it satisfies the request, reply with the deliverable verbatim. "
            "If it does not, list the specific gaps and ask the engineer to address them."
        ),
        agent=qa,
        expected_output="Either the approved deliverable or a gap list.",
        context=[build_task],
    )

    return Crew(
        agents=[researcher, engineer, qa],
        tasks=[research_task, build_task, qa_task],
        process=Process.hierarchical if settings.crewai_process == "hierarchical" else Process.sequential,
        manager_llm=sonnet if settings.crewai_process == "hierarchical" else None,
        verbose=False,
    )


def _run_sync(message: str, session_id: str) -> dict[str, Any]:
    if not settings.anthropic_api_key:
        return {
            "response": "[crewai error: ANTHROPIC_API_KEY not set]",
            "framework_used": "crewai",
            "session_id": session_id,
        }
    try:
        crew = _build_crew(message)
        result = crew.kickoff(inputs={"message": message})
    except Exception as e:
        return {
            "response": f"[crewai error: {str(e)[:300]}]",
            "framework_used": "crewai",
            "session_id": session_id,
        }

    raw = getattr(result, "raw", None) or str(result)
    return {
        "response": raw or "[crewai: empty result]",
        "framework_used": "crewai",
        "session_id": session_id,
    }


async def run_crew(message: str, session_id: str = "default") -> dict[str, Any]:
    """Run the hierarchical Researcher/Engineer/QA crew on a task."""
    return await asyncio.to_thread(_run_sync, message, session_id)
