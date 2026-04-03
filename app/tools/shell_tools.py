"""
Shell tools for Super Agent.

Read-only commands run freely.
Write commands (git push, git commit, rm, etc.) require the dispatcher
to confirm the owner safe word BEFORE calling run_authorized_shell_command.
"""

import subprocess
import shlex
from langchain.tools import tool

_WORKSPACE = "/workspace"

# Commands that are inherently read-only
_READ_ONLY_PREFIXES = (
    "ls", "cat", "head", "tail", "find", "grep", "git log",
    "git status", "git diff", "git branch", "git remote", "git show",
    "pwd", "echo", "env", "which", "whoami", "df", "du", "ps",
    "wc ", "sort", "uniq", "tree", "file ", "stat ",
    # Flutter/Dart inspection commands (read-only)
    "flutter doctor", "flutter --version", "flutter devices", "flutter pub get",
    "dart --version",
)


def _is_read_only(command: str) -> bool:
    stripped = command.strip().lower()
    return any(stripped.startswith(p) for p in _READ_ONLY_PREFIXES)


def _run(command: str, cwd: str, timeout: int) -> str:
    """
    Run a shell command. Always uses shell=True so that operators like >, |, &&,
    heredocs (<<), and pipelines work correctly. shlex.split() was intentionally
    removed — it broke any command containing shell operators.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            executable="/bin/bash",
        )
        output = result.stdout.strip() or result.stderr.strip()
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[Shell error: timed out after {timeout}s]"
    except FileNotFoundError as e:
        return f"[Shell error: command not found — {e}]"
    except Exception as e:
        return f"[Shell error: {e}]"


@tool
def run_shell_command(command: str) -> str:
    """
    Run a read-only shell command in the workspace (/workspace).
    Allowed: ls, cat, git log, git status, git diff, grep, find, etc.
    Write commands are automatically blocked — use run_authorized_shell_command instead.
    """
    if not _is_read_only(command):
        return (
            "[Blocked: this command modifies the system. "
            "The owner must authorize write operations with their safe word.]"
        )
    return _run(command, _WORKSPACE, timeout=30)


_BUILD_PREFIXES = ("flutter ", "gradle", "dart ", "npm ", "npx ", "pip install", "apt-get")


def _build_timeout(command: str) -> int:
    """Return a longer timeout for commands known to be slow (builds, installs)."""
    stripped = command.strip().lower()
    if any(stripped.startswith(p) for p in _BUILD_PREFIXES):
        return 600  # 10 minutes for build/install commands
    return 60


@tool
def run_authorized_shell_command(command: str) -> str:
    """
    Run any shell command in the workspace — including writes, git push, git commit, etc.
    Only callable after the dispatcher has verified the owner safe word.
    Build commands (flutter, gradle, npm, pip install) automatically get a 10-minute timeout.
    Supports all shell operators: >, >>, |, &&, etc.

    IMPORTANT: Heredoc syntax (<<) is BLOCKED for writing Dart/YAML/XML/JSON files.
    Use write_workspace_file(file_path, content) for ANY file write with multi-line content.
    Heredoc breaks on Dart's $, {}, and single-quote characters.
    """
    # Block heredoc usage for file writing — it silently corrupts Dart/YAML content
    cmd_lower = command.strip().lower()
    if "<<" in command and any(ext in cmd_lower for ext in (
        ".dart", "pubspec", ".yaml", ".yml", ".xml", "manifest", ".gradle", ".json", ".kt"
    )):
        return (
            "[BLOCKED] Do NOT use heredoc (<<) to write source files — it corrupts Dart/YAML/XML content.\n"
            "Use write_workspace_file(file_path, content) instead. Example:\n"
            "  write_workspace_file('super_agent_voice/lib/main.dart', '<full dart code here>')\n"
            "write_workspace_file writes via Python file I/O and handles all special characters correctly."
        )
    return _run(command, _WORKSPACE, timeout=_build_timeout(command))


@tool
def write_workspace_file(file_path: str, content: str) -> str:
    """
    Write content directly to a file in /workspace using Python file I/O.
    Use this instead of shell heredoc (cat > file << 'EOF') for ANY file larger than
    a few lines — heredocs break with special characters, long content, or shell operators.

    file_path: path relative to /workspace (e.g. 'super_agent_voice/lib/main.dart')
               OR an absolute path starting with /workspace or /opt.
    content: the full file content to write (UTF-8).

    Returns confirmation with byte count written.
    """
    from pathlib import Path as _Path
    try:
        if file_path.startswith("/"):
            fp = _Path(file_path)
        else:
            fp = _Path(_WORKSPACE) / file_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        size = fp.stat().st_size
        return f"Written {size} bytes to {fp}"
    except Exception as e:
        return f"[write_workspace_file error] {e}"


@tool
def clone_repo(repo_name: str) -> str:
    """
    Clone a GitHub repository into /workspace.
    repo_name format: 'owner/repo'  e.g. 'gelson/my-project'
    """
    url = f"https://github.com/{repo_name}.git"
    target = f"{_WORKSPACE}/{repo_name.split('/')[-1]}"
    return _run(f"git clone {url} {target}", "/workspace", timeout=120)


@tool
def list_workspace() -> str:
    """List all directories and files currently in /workspace (cloned repos)."""
    return _run("ls -la /workspace", "/workspace", timeout=10)


@tool
def run_claude_cli(prompt: str) -> str:
    """
    Run the Claude Code CLI in non-interactive mode with the given prompt.
    Uses the ANTHROPIC_API_KEY from environment.
    Best for: code review, auto-fix suggestions, and explaining code in a repo.
    """
    return _run(f'claude -p "{prompt}"', _WORKSPACE, timeout=120)
