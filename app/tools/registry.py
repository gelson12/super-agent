"""
Centralized tool registry — TOOLSETS keyed by agent role.

Source of truth is the existing @tool-decorated functions in app/tools/*.py.
This module composes them into role-shaped bundles that the new orchestration
frameworks (LangGraph custom graphs, CrewAI, AutoGen) can import.

The four legacy ReAct agents in app/agents/*_agent.py still build their tool
lists locally — the registry mirrors those bundles so the new frameworks see
the same capabilities. Kept in sync manually; if a legacy agent gains a tool,
add it here too.
"""
from __future__ import annotations

from .github_tools import (
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
)
from .shell_tools import (
    run_shell_command,
    run_authorized_shell_command,
    write_workspace_file,
    clone_repo,
    list_workspace,
    run_claude_cli,
    run_shell_via_cli_worker,
)
from .flutter_tools import (
    build_flutter_voice_app,
    flutter_create_project,
    flutter_build_apk,
    upload_build_artifact,
    flutter_git_push,
    flutter_test,
    retry_apk_upload,
    regenerate_apk_download_link,
)
from .n8n_tools import (
    n8n_list_workflows,
    n8n_get_workflow,
    n8n_create_workflow,
    n8n_update_workflow,
    n8n_delete_workflow,
    n8n_cleanup_test_workflows,
    n8n_activate_workflow,
    n8n_deactivate_workflow,
    n8n_execute_workflow,
    n8n_list_executions,
    n8n_get_execution,
)
from .railway_tools import (
    railway_list_services,
    railway_list_variables,
    railway_get_logs,
    railway_get_deployment_status,
    railway_redeploy,
    railway_set_variable,
)
from .database_tools import (
    db_health_check,
    db_list_sessions,
    db_get_error_stats,
    db_get_failure_patterns,
    db_clear_session,
    db_run_safe_query,
)
from .obsidian_tools import OBSIDIAN_TOOLS
from .secretary_tools import SECRETARY_TOOLS
from .search_tools import web_search


_GITHUB = [
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
    railway_list_variables,
    railway_get_logs,
    railway_get_deployment_status,
    run_shell_command,
    *OBSIDIAN_TOOLS,
]

_SHELL = [
    run_shell_command,
    run_authorized_shell_command,
    write_workspace_file,
    clone_repo,
    list_workspace,
    run_claude_cli,
    build_flutter_voice_app,
    flutter_create_project,
    flutter_build_apk,
    upload_build_artifact,
    flutter_git_push,
    flutter_test,
    retry_apk_upload,
    regenerate_apk_download_link,
    railway_list_services,
    railway_get_logs,
    railway_get_deployment_status,
    railway_list_variables,
    railway_redeploy,
    db_health_check,
    db_get_error_stats,
    db_get_failure_patterns,
    *OBSIDIAN_TOOLS,
    *SECRETARY_TOOLS,
]

_N8N = [
    n8n_list_workflows,
    n8n_get_workflow,
    n8n_create_workflow,
    n8n_update_workflow,
    n8n_delete_workflow,
    n8n_cleanup_test_workflows,
    n8n_activate_workflow,
    n8n_deactivate_workflow,
    n8n_execute_workflow,
    n8n_list_executions,
    n8n_get_execution,
    railway_list_services,
    railway_get_logs,
    railway_get_deployment_status,
    railway_list_variables,
    railway_redeploy,
    run_shell_via_cli_worker,
    run_authorized_shell_command,
    *OBSIDIAN_TOOLS,
]

_SELF_IMPROVE = [
    *_GITHUB,
    *_SHELL,
    *_N8N,
    railway_set_variable,
    db_list_sessions,
    db_clear_session,
    db_run_safe_query,
    web_search,
]

_RESEARCH = [
    web_search,
    github_list_repos,
    github_list_files,
    github_read_file,
    db_health_check,
    db_get_error_stats,
    db_get_failure_patterns,
    *OBSIDIAN_TOOLS,
]

_ENGINEERING = [
    github_list_repos,
    github_read_file,
    github_create_or_update_file,
    github_create_branch,
    github_create_pull_request,
    run_shell_command,
    write_workspace_file,
    clone_repo,
    list_workspace,
    flutter_create_project,
    flutter_build_apk,
    flutter_test,
    *OBSIDIAN_TOOLS,
]


TOOLSETS: dict[str, list] = {
    "github": _GITHUB,
    "shell": _SHELL,
    "n8n": _N8N,
    "self_improve": _SELF_IMPROVE,
    "research": _RESEARCH,
    "engineering": _ENGINEERING,
}


def get_toolset(name: str) -> list:
    if name not in TOOLSETS:
        raise KeyError(f"unknown toolset {name!r}; valid: {sorted(TOOLSETS)}")
    return TOOLSETS[name]


def list_toolsets() -> dict[str, int]:
    return {k: len(v) for k, v in TOOLSETS.items()}
