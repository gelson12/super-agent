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
from ..tools.flutter_tools import (
    build_flutter_voice_app,
    flutter_create_project,
    flutter_build_apk,
    upload_build_artifact,
    flutter_git_push,
    flutter_test,
    retry_apk_upload,
    regenerate_apk_download_link,
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

## EXECUTION STANCE — NON-NEGOTIABLE RULES
  • NEVER ask the user clarifying questions. NEVER ask "what is the exact task?",
    "which build target?", "should I upload to Cloudinary?", or anything similar.
    If a request is ambiguous, use the most obvious interpretation and execute.
  • NEVER check workspace state before acting on a build request. Do not run
    list_workspace, ls, or any inspection command before calling build tools.
  • For ANY request containing "voice app", "android app", "apk", "build app",
    or "download link": call build_flutter_voice_app() IMMEDIATELY as your first action.
    This tool handles EVERYTHING in one call: scaffold → pubspec → main.dart →
    manifest → pub get → build APK → upload → return download URL.
    Do NOT scaffold manually. Do NOT write files manually. Just call it.
  • For other APK builds: flutter_create_project → write_workspace_file → flutter_build_apk
  • For write operations (git push, git commit, file writes): the owner safe word was already verified.

## WRITING FILES — CRITICAL RULE
  NEVER use shell heredoc (cat > file << 'EOF') for any file larger than 3 lines.
  ALWAYS use write_workspace_file(file_path, content) for Dart/YAML/XML files.

## DEBUGGING STANCE
  Isolate → Identify → Fix → Integrate. Never debug the whole system at once.
  When asked to fix code: read relevant files first → apply fix directly (authorization already given).
Keep responses concise. Return the download URL and install steps — nothing else."""


def run_shell_agent(message: str, authorized: bool = False, debug_mode: bool = False) -> str:
    """
    Run the shell agent with pre-execution model competition + self-healing.

    Args:
        message:    The user's request.
        authorized: True if the owner safe word was verified — enables write tools.
        debug_mode: True when routed as isolation_debug — prepends ISOLATION_DEBUG_PROMPT.
    """
    tools = [
        run_shell_command, write_workspace_file, clone_repo, list_workspace, run_claude_cli,
        # Flutter build pipeline — use build_flutter_voice_app for voice app requests
        build_flutter_voice_app, flutter_create_project, flutter_build_apk,
        upload_build_artifact, flutter_git_push, flutter_test,
        retry_apk_upload, regenerate_apk_download_link,
        # Infrastructure visibility — always available for self-healing
        railway_list_services, railway_get_logs, railway_get_deployment_status,
        railway_list_variables,
        db_health_check, db_get_error_stats, db_get_failure_patterns,
    ]
    if authorized:
        tools.append(run_authorized_shell_command)
        tools.append(railway_redeploy)

    # Inject winning build recipe if one exists — agent replays what worked last time
    _recipe_hint = ""
    if any(kw in message.lower() for kw in ("build", "apk", "flutter", "voice app")):
        try:
            from ..learning.build_recipes import build_context_hint
            _recipe_hint = build_context_hint("super_agent_voice")
        except Exception:
            pass

    base_content = f"{_recipe_hint}\n\n{message}".strip() if _recipe_hint else message
    user_content = f"{ISOLATION_DEBUG_PROMPT}\n\n---\n\n{base_content}" if debug_mode else base_content

    # ── 1. Claude CLI (zero API cost, preferred) ──────────────────────────────
    try:
        from ..learning.pro_router import try_pro
        cli_result = try_pro(f"{_SYSTEM_PROMPT}\n\n{user_content}")
        if cli_result and not cli_result.startswith("["):
            return cli_result
    except Exception:
        pass

    # ── 2. Gemini CLI (free fallback) ─────────────────────────────────────────
    # Gemini has no shell tool access but can answer informational queries.
    # Always try it before spending API credits.
    try:
        from ..learning.gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(f"{_SYSTEM_PROMPT}\n\n{user_content}")
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass

    # ── 3. LangGraph + Anthropic API — full shell + infrastructure tools ──────
    try:
        from ..activity_log import bg_log as _bg
        from langchain_anthropic import ChatAnthropic
        from langgraph.prebuilt import create_react_agent
        _bg("Shell agent: using LangGraph (Anthropic API) — CLI/Gemini unavailable", source="shell_agent")
        _llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=settings.anthropic_api_key, max_tokens=8192)
        _agent = create_react_agent(_llm, tools)
        _result = _agent.invoke({
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]
        })
        return extract_final_agent_text(_result) or "[shell agent: no response]"
    except Exception as _e:
        return f"⚠️ Shell agent error: {_e}"
