"""
Shell Agent — gives Super Agent terminal + Claude CLI access to /workspace.

Read-only tools are always available.
Write tools (run_authorized_shell_command) are only included when the dispatcher
has already confirmed the owner safe word in the original message.
"""

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

from ..config import settings
from ..prompts import ISOLATION_DEBUG_PROMPT
from .agent_planner import run_with_plan_and_recovery
from ..tools.shell_tools import (
    run_shell_command,
    run_authorized_shell_command,
    clone_repo,
    list_workspace,
    run_claude_cli,
)

_SYSTEM_PROMPT = """You are Super Agent's terminal interface with LIVE access to a Linux workspace.

RUNTIME ENVIRONMENT (all paths confirmed present on this Railway container):
  Flutter SDK : /opt/flutter/bin/flutter  (Flutter 3.27.4)
  Android SDK : /opt/android-sdk  (platforms;android-34, build-tools;34.0.0)
  Java        : /usr/lib/jvm/default-java
  Workspace   : /workspace  (clone repos here, run builds here)
  GitHub PAT  : configured — use clone_repo to clone from github.com/gelson12/*
  Claude CLI  : available as 'claude' in /workspace

EXECUTION STANCE:
  • Execute immediately — never ask "do I have tool access?", you always do.
  • For APK builds: clone repo → flutter pub get → flutter build apk --debug
  • APK output: <project>/build/app/outputs/flutter-apk/app-debug.apk
  • After a build, upload the APK to Cloudinary if the upload_build_artifact tool is available.
  • For write operations (git push, git commit, file writes): the owner safe word was already verified.

DEBUGGING STANCE (for error/not-working requests):
  Isolate → Identify → Fix → Integrate. Never debug the whole system at once.

Always confirm which repo/directory you are in before running commands.
When asked to fix code: read the relevant files first → propose the fix → apply only if authorized.
Keep responses concise and action-oriented."""


def run_shell_agent(message: str, authorized: bool = False, debug_mode: bool = False) -> str:
    """
    Run the shell agent with pre-execution model competition + self-healing.

    Args:
        message:    The user's request.
        authorized: True if the owner safe word was verified — enables write tools.
        debug_mode: True when routed as isolation_debug — prepends ISOLATION_DEBUG_PROMPT.
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

    user_content = f"{ISOLATION_DEBUG_PROMPT}\n\n---\n\n{message}" if debug_mode else message

    def _invoke(msg: str) -> str:
        result = agent.invoke({
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": msg},
            ]
        })
        for m in reversed(result.get("messages", [])):
            text = getattr(m, "content", "")
            if isinstance(text, str) and text.strip():
                return text.strip()
        return "[Shell agent returned no response]"

    return run_with_plan_and_recovery(
        agent_fn=_invoke,
        message=user_content,
        agent_type="shell_agent",
        tool_names=[t.name for t in tools],
    )
