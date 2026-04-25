"""
Plan → Execute → Critique → Retry StateGraph.

A custom LangGraph workflow (vs. the prebuilt ReAct agents in app/agents/*).
Each node reuses an existing super-agent helper:
  - classify  → app.routing.classifier.classify_request
  - plan      → app.agents.agent_planner.compete_and_plan
  - execute   → app.agents.agent_routing.tiered_agent_invoke
  - critique  → app.learning.internal_llm.ask_internal_fast (Haiku)
  - retry     → conditional edge back to plan if verdict=RETRY (max 2 times)

Checkpointing uses PostgresSaver when DATABASE_URL is set (Railway), falling
back to MemorySaver for local/dev. The thread_id passed to run_graph() lets a
follow-up call resume mid-graph instead of starting over.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from .state import WorkflowState
from ...config import settings
from ..agent_planner import compete_and_plan
from ..agent_routing import tiered_agent_invoke


_MAX_RETRIES = 2

# Map classifier output to a TOOLSETS key.
_CLASS_TO_TOOLSET = {
    "GITHUB": "github",
    "SHELL": "shell",
    "N8N": "n8n",
    "SELF_IMPROVE": "self_improve",
    "SEARCH": "research",
    "GENERAL": "research",
    # classify_request returns model labels (CLAUDE/HAIKU/...) when called
    # standalone, but we wrap with our own keyword-first router below.
}


def _route_keyword(message: str) -> str:
    """Cheap keyword router — covers the common cases without an LLM call."""
    lower = message.lower()
    if any(k in lower for k in ("github", "repo", "pull request", "commit", "branch")):
        return "github"
    if any(k in lower for k in ("n8n", "workflow", "webhook")):
        return "n8n"
    if any(k in lower for k in ("flutter", "apk", "shell", "build", "deploy", "supervisor")):
        return "shell"
    if any(k in lower for k in ("fix", "diagnose", "self", "improve", "self-heal")):
        return "self_improve"
    if any(k in lower for k in ("search", "research", "what is", "who is", "explain")):
        return "research"
    return "engineering"


def _node_classify(state: WorkflowState) -> WorkflowState:
    msg = state["message"]
    return {"classification": _route_keyword(msg)}


def _node_plan(state: WorkflowState) -> WorkflowState:
    from ...tools.registry import get_toolset

    toolset = get_toolset(state["classification"])
    tool_names = [getattr(t, "name", "?") for t in toolset]
    try:
        plan = compete_and_plan(state["message"], state["classification"], tool_names)
    except Exception as e:
        plan = f"[plan fallback: {e}] Execute directly: {state['message']}"
    return {"plan": plan}


def _node_execute(state: WorkflowState) -> WorkflowState:
    from ...tools.registry import get_toolset

    toolset = get_toolset(state["classification"])
    augmented = (
        f"[EXECUTION PLAN]\n{state.get('plan', '')}\n"
        f"{'-' * 60}\n"
        f"TASK: {state['message']}\n\n"
        f"Execute the plan phase by phase. Adapt on errors."
    )
    system = (
        "You are a Super-Agent worker running inside a LangGraph workflow. "
        "Execute the plan above using the tools provided. Report concrete results."
    )
    try:
        out = tiered_agent_invoke(
            message=augmented,
            system_prompt=system,
            tools=toolset,
            agent_type=state["classification"],
            source="graph_runner",
        )
    except Exception as e:
        out = f"[execute error: {str(e)[:200]}]"
    return {"execution": out}


_CRITIQUE_PROMPT = """\
You are a strict reviewer. Decide whether the execution result below adequately
satisfies the user's task. Look for: missing steps, hallucinated success,
unaddressed errors, off-topic content.

TASK: {task}

EXECUTION RESULT:
{result}

Reply in exactly two lines:
VERDICT: APPROVED   (if the result is acceptable)
        OR
VERDICT: RETRY      (if a retry would meaningfully improve the result)
NOTES: <one sentence — what to improve, or why approved>
"""


def _node_critique(state: WorkflowState) -> WorkflowState:
    from ...learning.internal_llm import ask_internal_fast as ask_haiku

    prompt = _CRITIQUE_PROMPT.format(
        task=state["message"],
        result=(state.get("execution") or "")[:4000],
    )
    try:
        review = ask_haiku(prompt) or ""
    except Exception as e:
        review = f"VERDICT: APPROVED\nNOTES: critique skipped ({e})"

    verdict = "APPROVED"
    for line in review.splitlines():
        s = line.strip().upper()
        if s.startswith("VERDICT:"):
            verdict = "RETRY" if "RETRY" in s else "APPROVED"
            break

    return {"critique": review, "verdict": verdict}


def _node_finish(state: WorkflowState) -> WorkflowState:
    final = state.get("execution") or ""
    notes = state.get("critique", "")
    if state.get("retries", 0) > 0:
        final = f"{final}\n\n_(After {state['retries']} retry/retries — reviewer notes: {notes})_"
    return {"final": final}


def _maybe_retry(state: WorkflowState) -> str:
    if state.get("verdict") == "RETRY" and state.get("retries", 0) < _MAX_RETRIES:
        return "retry"
    return "done"


def _node_retry_bump(state: WorkflowState) -> WorkflowState:
    return {"retries": state.get("retries", 0) + 1}


def _build_checkpointer():
    """Return a checkpointer — Postgres if DATABASE_URL is set, else in-memory."""
    dsn = settings.langgraph_checkpointer_dsn or settings.database_url
    if not dsn:
        return MemorySaver()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        normalized = dsn.replace("postgres://", "postgresql://", 1)
        # PostgresSaver.from_conn_string returns a context manager in newer versions;
        # we keep a singleton to avoid reopening per request.
        cm = PostgresSaver.from_conn_string(normalized)
        saver = cm.__enter__()
        try:
            saver.setup()
        except Exception:
            pass
        return saver
    except Exception as e:
        from ...activity_log import bg_log
        try:
            bg_log(f"PostgresSaver unavailable, falling back to memory: {e}", source="graph_runner")
        except Exception:
            pass
        return MemorySaver()


_GRAPH = None
_CHECKPOINTER = None


def get_graph():
    """Build the StateGraph once, reuse across requests."""
    global _GRAPH, _CHECKPOINTER
    if _GRAPH is not None:
        return _GRAPH

    g = StateGraph(WorkflowState)
    g.add_node("classify", _node_classify)
    g.add_node("plan", _node_plan)
    g.add_node("execute", _node_execute)
    g.add_node("critique", _node_critique)
    g.add_node("retry_bump", _node_retry_bump)
    g.add_node("finish", _node_finish)

    g.add_edge(START, "classify")
    g.add_edge("classify", "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "critique")
    g.add_conditional_edges("critique", _maybe_retry, {"retry": "retry_bump", "done": "finish"})
    g.add_edge("retry_bump", "plan")
    g.add_edge("finish", END)

    _CHECKPOINTER = _build_checkpointer()
    _GRAPH = g.compile(checkpointer=_CHECKPOINTER)
    return _GRAPH
