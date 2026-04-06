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
    Checks the shared ResponseCache first (TTL 1h) — on a hit the subprocess
    is skipped entirely, saving Pro quota.  On a miss the response is cached
    for future identical prompts.  Usage is recorded to pro_usage_tracker
    regardless of cache status.

    Returns the response string. Never raises — returns an error string
    so ThreadPoolExecutor competitors keep running on failure.
    """
    # ── Cache lookup ──────────────────────────────────────────────────────────
    try:
        from ..cache.response_cache import cache as _cache
        _hit = _cache.get(prompt, "PRO_CLI", ttl=3600)
        if _hit:
            try:
                from .pro_usage_tracker import record as _pro_record
                _pro_record(len(prompt), len(_hit), was_cached=True)
            except Exception:
                pass
            return _hit
    except Exception:
        pass

    # ── Gemini fallback when Pro daily limit is hit ───────────────────────────
    try:
        from .pro_router import is_pro_available
        if not is_pro_available():
            from .gemini_cli_worker import ask_gemini_cli
            return ask_gemini_cli(prompt)
    except Exception:
        pass

    # ── Subprocess call ───────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd="/workspace",
        )
        output = result.stdout.strip() or result.stderr.strip() or "(no output)"

        # Cache successful (non-error) responses for future identical prompts
        if not output.startswith("["):
            try:
                from ..cache.response_cache import cache as _cache
                _cache.set(prompt, "PRO_CLI", output)
            except Exception:
                pass

        # Record Pro quota usage
        try:
            from .pro_usage_tracker import record as _pro_record
            _pro_record(len(prompt), len(output), was_cached=False)
        except Exception:
            pass

        return output
    except subprocess.TimeoutExpired:
        # Pro timed out — try Gemini as a fallback before giving up
        try:
            from .gemini_cli_worker import ask_gemini_cli
            gemini_result = ask_gemini_cli(prompt)
            if not gemini_result.startswith("["):
                return gemini_result
        except Exception:
            pass
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
