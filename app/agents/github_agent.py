from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from ..config import settings
from .agent_planner import run_with_plan_and_recovery
from ..tools.github_tools import (
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
)

_SYSTEM = """You are a GitHub assistant with LIVE access to Gelson's GitHub account (gelson12).

EXECUTION STANCE: Execute immediately. Never say 'I don't have access' — GITHUB_PAT is configured and live.

REPO DISCOVERY: If the user does not specify a repo name, call github_list_repos first to discover
all available repos, then choose the most relevant one based on the task context.

You can:
- List all repositories under gelson12 (use this when repo name is unknown)
- Read any file in any repo
- Create, update, or delete files (with a commit message)
- Create branches
- Open pull requests

Always confirm the exact action taken and its result. Use clear, descriptive commit messages.
When unsure of a branch name, try 'main' first then 'master'.
Never guess file content — read the file first if you need to modify it."""

_GITHUB_TOOLS = [
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
]

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=settings.anthropic_api_key,
            max_tokens=settings.max_tokens_claude,
        )
        _agent = create_react_agent(llm, _GITHUB_TOOLS)
    return _agent


def _invoke(message: str) -> str:
    """Raw agent invoke — called by run_with_plan_and_recovery."""
    agent = _get_agent()
    result = agent.invoke({
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": message},
        ]
    })
    for msg in reversed(result.get("messages", [])):
        if hasattr(msg, "type") and msg.type in ("ai", "assistant"):
            content = msg.content
            if isinstance(content, list):
                return " ".join(
                    block.get("text", "") for block in content if isinstance(block, dict)
                ).strip()
            return str(content).strip()
    return "[GitHub agent: no response]"


def run_github_agent(message: str) -> str:
    """
    Run the GitHub agent with pre-execution model competition + self-healing.

    Pipeline:
      1. Claude vs DeepSeek compete on execution plan
      2. Haiku adjudicates — winning plan injected into agent context
      3. Agent executes; on failure → diagnose → SAFE auto-fix or CRITICAL safe-word prompt
      4. Up to 3 self-healing retries before escalating
    """
    if not settings.github_pat:
        return "[GitHub agent error: GITHUB_PAT not set]"

    return run_with_plan_and_recovery(
        agent_fn=_invoke,
        message=message,
        agent_type="github_agent",
        tool_names=[t.name for t in _GITHUB_TOOLS],
    )
