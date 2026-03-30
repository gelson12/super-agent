"""
Shell Agent — gives Super Agent terminal + Claude CLI access to /workspace.

Read-only tools are always available.
Write tools (run_authorized_shell_command) are only included when the dispatcher
has already confirmed the owner safe word in the original message.
"""

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

from ..config import settings
from ..tools.shell_tools import (
    run_shell_command,
    run_authorized_shell_command,
    clone_repo,
    list_workspace,
    run_claude_cli,
)

_SYSTEM_PROMPT = """You are Super Agent's terminal interface with access to a Linux workspace (/workspace) \
where GitHub repositories can be cloned and worked on.

You have access to shell tools to:
- List, read, and search files in cloned repositories
- Clone GitHub repositories
- Run git log, git diff, git status to inspect repo state
- Use the Claude CLI for code review and auto-fix suggestions
- Execute authorized write commands (git commit, git push, file writes) when owner-authorized

Always confirm which repo/directory you are working in before running commands.
When asked to fix code, first read the relevant files, then propose the fix, then apply it if authorized.
Keep responses concise and action-oriented."""


def run_shell_agent(message: str, authorized: bool = False) -> str:
    """
    Run the shell agent.

    Args:
        message:    The user's request.
        authorized: True if the owner safe word was verified — enables write tools.
    """
    if not settings.anthropic_api_key:
        return "[Shell agent error: ANTHROPIC_API_KEY not set]"

    tools = [run_shell_command, clone_repo, list_workspace, run_claude_cli]
    if authorized:
        tools.append(run_authorized_shell_command)

    llm = ChatAnthropic(
        model="claude-haiku-4-5-20251001",
        api_key=settings.anthropic_api_key,
        max_tokens=2048,
    )

    agent = create_react_agent(llm, tools)

    try:
        result = agent.invoke({
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ]
        })
        msgs = result.get("messages", [])
        for msg in reversed(msgs):
            text = getattr(msg, "content", "")
            if isinstance(text, str) and text.strip():
                return text.strip()
        return "[Shell agent returned no response]"
    except Exception as e:
        return f"[Shell agent error: {e}]"
