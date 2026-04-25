"""
Public entry point for the LangGraph custom workflow.

run_graph(message, session_id, thread_id=None) → dict
    {"response": str, "framework_used": "langgraph", "thread_id": str,
     "classification": str, "retries": int}

thread_id is the LangGraph checkpoint key. Pass it again in a follow-up call to
resume from the last checkpoint instead of starting over. Defaults to session_id
when not provided.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .plan_execute_critique import get_graph


def _run_sync(message: str, session_id: str, thread_id: str | None) -> dict[str, Any]:
    graph = get_graph()
    tid = thread_id or session_id or f"graph-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": tid}}
    initial = {"message": message, "session_id": session_id, "retries": 0}
    final_state = graph.invoke(initial, config=config)
    return {
        "response": final_state.get("final") or final_state.get("execution") or "[graph: no output]",
        "framework_used": "langgraph",
        "thread_id": tid,
        "classification": final_state.get("classification", ""),
        "retries": final_state.get("retries", 0),
    }


async def run_graph(message: str, session_id: str = "default", thread_id: str | None = None) -> dict[str, Any]:
    """Run the StateGraph in a worker thread (graph nodes are blocking)."""
    return await asyncio.to_thread(_run_sync, message, session_id, thread_id)
