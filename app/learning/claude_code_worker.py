"""
Claude Code CLI worker — subprocess wrapper + insight logger.

ask_claude_code(prompt) runs `claude -p "..."` inside /workspace,
giving the model access to actual files on disk (unlike API-only models).

log_claude_code_result() persists outcomes to /workspace/claude_code_insights.json
so Super Agent can learn which model wins which task type over time.
"""
import datetime
import json
import subprocess
from pathlib import Path

_INSIGHTS_FILE = Path("/workspace/claude_code_insights.json")
_TIMEOUT = 120


def ask_claude_code(prompt: str) -> str:
    """
    Run the Claude Code CLI non-interactively with the given prompt.
    Returns the response string. Never raises — returns an error string
    so ThreadPoolExecutor competitors keep running on failure.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd="/workspace",
        )
        return result.stdout.strip() or result.stderr.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[claude_code: timed out after {_TIMEOUT}s]"
    except FileNotFoundError:
        return "[claude_code: claude CLI not found in container]"
    except Exception as e:
        return f"[claude_code error: {e}]"


def log_claude_code_result(
    prompt: str,
    response: str,
    was_winner: bool,
    agent_type: str,
) -> None:
    """
    Append one competition record to /workspace/claude_code_insights.json.
    Best-effort — never raises, never blocks the caller.
    Keeps the last 500 records to prevent unbounded growth.
    """
    try:
        records = (
            json.loads(_INSIGHTS_FILE.read_text())
            if _INSIGHTS_FILE.exists()
            else []
        )
        records.append({
            "ts": datetime.datetime.utcnow().isoformat(),
            "agent_type": agent_type,
            "prompt_preview": prompt[:200],
            "response_preview": response[:200],
            "was_winner": was_winner,
        })
        _INSIGHTS_FILE.write_text(json.dumps(records[-500:], indent=2))
    except Exception:
        pass
