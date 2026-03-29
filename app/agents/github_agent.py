from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from ..config import settings
from ..tools.github_tools import (
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
)

_SYSTEM = """You are a GitHub assistant with full access to Gelson's GitHub account (gelson12).

You can:
- List all repositories
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


def run_github_agent(message: str) -> str:
    """Run the GitHub ReAct agent and return the final text response."""
    if not settings.github_pat:
        return "[GitHub agent error: GITHUB_PAT not set]"

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
