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
from .agent_planner import run_with_plan_and_recovery, extract_final_agent_text
from ..tools.shell_tools import (
    run_shell_command,
    run_authorized_shell_command,
    write_workspace_file,
    clone_repo,
    list_workspace,
    run_claude_cli,
)
from ..tools.railway_tools import (
    railway_list_services,
    railway_get_logs,
    railway_get_deployment_status,
    railway_list_variables,
    railway_redeploy,
)
from ..tools.database_tools import (
    db_health_check,
    db_get_error_stats,
    db_get_failure_patterns,
)

_SYSTEM_PROMPT = """You are Super Agent's terminal interface with LIVE access to a Linux workspace AND full Railway infrastructure visibility.

RUNTIME ENVIRONMENT (all paths confirmed present on this Railway container):
  Flutter SDK : /opt/flutter/bin/flutter  (Flutter 3.27.4)
  Android SDK : /opt/android-sdk  (platforms;android-34, build-tools;34.0.0)
  Java        : /usr/lib/jvm/default-java
  Workspace   : /workspace  (clone repos here, run builds here)
  GitHub PAT  : configured — use clone_repo to clone from github.com/gelson12/*
  Claude CLI  : available as 'claude' in /workspace
  VS Code     : code-server running on port 3001, accessible at /code/ via nginx
  Supervisor  : /usr/bin/supervisord manages nginx + uvicorn + code-server

## SELF-HEALING — MANDATORY WHEN SERVICES FAIL

If any shell command or service check reveals a failure:

1. VS CODE / CODE-SERVER DOWN (port 3001 not responding):
   → run_shell_command: "supervisorctl status" to see all service states
   → run_shell_command: "supervisorctl restart code-server" to restart it
   → run_shell_command: "curl -s http://127.0.0.1:3001/" to verify recovery
   → Check railway_get_logs for startup errors if restart fails

2. UVICORN / SUPER AGENT API DOWN (port 8001 not responding):
   → run_shell_command: "supervisorctl status uvicorn"
   → run_shell_command: "supervisorctl restart uvicorn"
   → Check railway_get_logs for Python errors

3. DATABASE CONNECTION FAILURES:
   → Call db_health_check to confirm DB state
   → Call db_get_failure_patterns to see what's been failing
   → Call railway_list_variables to confirm DATABASE_URL is set

4. RAILWAY DEPLOYMENT ISSUES:
   → railway_get_deployment_status → is the service running?
   → railway_get_logs → what was the last error?
   → If crashed: railway_redeploy triggers a fresh deploy

5. CLOUDINARY UNREACHABLE:
   → run_shell_command: "curl -s https://api.cloudinary.com/v1_1/ping" to test connectivity
   → railway_list_variables to confirm CLOUDINARY_* vars are set
   → Report exact variable names found (never values)

NEVER tell the user to SSH into the server, go to a dashboard, or restart manually.
Use supervisorctl, railway tools, and shell commands to investigate and fix autonomously.

## EXECUTION STANCE
  • Execute immediately — never ask "do I have tool access?", you always do.
  • For APK builds: clone repo → flutter pub get → flutter build apk --debug
  • APK output: <project>/build/app/outputs/flutter-apk/app-debug.apk
  • After a build, upload the APK to Cloudinary if the upload_build_artifact tool is available.
  • For write operations (git push, git commit, file writes): the owner safe word was already verified.

## WRITING FILES — CRITICAL RULE
  NEVER use shell heredoc (cat > file << 'EOF') for any file larger than 3 lines.
  Heredocs break with Dart/Kotlin/YAML special characters and long content.
  ALWAYS use write_workspace_file(file_path, content) for:
    - Dart source files (main.dart, any .dart)
    - pubspec.yaml, AndroidManifest.xml, build.gradle
    - Any file with quotes, backslashes, or multiline strings
  write_workspace_file writes via Python file I/O — it never fails on content length or special chars.

## DEBUGGING STANCE
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

    tools = [
        run_shell_command, write_workspace_file, clone_repo, list_workspace, run_claude_cli,
        # Infrastructure visibility — always available for self-healing
        railway_list_services, railway_get_logs, railway_get_deployment_status,
        railway_list_variables,
        db_health_check, db_get_error_stats, db_get_failure_patterns,
    ]
    if authorized:
        tools.append(run_authorized_shell_command)
        tools.append(railway_redeploy)

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
        text = extract_final_agent_text(result)
        return text or "[Shell agent returned no response]"

    return run_with_plan_and_recovery(
        agent_fn=_invoke,
        message=user_content,
        agent_type="shell_agent",
        tool_names=[t.name for t in tools],
    )
