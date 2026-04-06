"""
Gemini CLI Worker — backup for when Claude Pro CLI hits its daily limit.

Uses the Google Gemini CLI (free tier via Google account OAuth) as a
zero-cost fallback for nightly reviews, health checks, and voting calls.

Free tier:  ~1,500 requests/day with Gemini 2.5 Pro via Google account
Install:    npm install -g @google/gemini-cli
Auth:       gemini auth login  (Google account OAuth, no credit card)
Creds:      /root/.gemini/credentials.json  (decoded from GEMINI_SESSION_TOKEN)

Usage is tracked in pro_usage_tracker with model="GEMINI_CLI" so the
/pro-usage endpoint shows combined quota consumption.

Never raises — returns an error string on failure so callers can handle it.
"""
import subprocess
import os

_TIMEOUT = 120
_CREDS_DIR = "/root/.gemini"


def is_gemini_cli_available() -> bool:
    """Return True if the gemini CLI binary is installed and credentials exist."""
    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HOME": "/root"},
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def ask_gemini_cli(prompt: str) -> str:
    """
    Run the Google Gemini CLI non-interactively with the given prompt.

    Returns the response string. Never raises — returns an error string
    prefixed with [ so callers know it's an error (same contract as ask_claude_code).
    """
    try:
        # Try response cache first (same TTL as Claude CLI cache)
        try:
            from ..cache.response_cache import cache as _cache
            _hit = _cache.get(prompt, "GEMINI_CLI", ttl=3600)
            if _hit:
                return _hit
        except Exception:
            pass

        result = subprocess.run(
            ["gemini", "--prompt", prompt],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd="/workspace",
            env={**os.environ, "HOME": "/root"},
        )
        output = result.stdout.strip() or result.stderr.strip() or "(no output)"

        # Cache and track on success
        if not output.startswith("["):
            try:
                from ..cache.response_cache import cache as _cache
                _cache.set(prompt, "GEMINI_CLI", output)
            except Exception:
                pass
            try:
                from .pro_usage_tracker import record as _pro_record
                _pro_record(len(prompt), len(output), was_cached=False)
            except Exception:
                pass

        return output

    except subprocess.TimeoutExpired:
        return f"[gemini_cli: timed out after {_TIMEOUT}s]"
    except FileNotFoundError:
        return "[gemini_cli: gemini CLI not found in container]"
    except Exception as e:
        return f"[gemini_cli error: {e}]"
