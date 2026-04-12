"""
Safe word authorization guard.

Critical operations (GitHub writes, shell write commands) are blocked unless
the owner's safe word is present in the request message.

n8n workflow operations are EXEMPT — the n8n instance is already protected
by its own API key, and the n8n agent is behind the dispatcher's routing
so external users cannot directly call n8n tools.

The safe word is stored in the OWNER_SAFE_WORD environment variable.
Default: alpha0  (change via Railway env var — do NOT commit the real word).
"""

from ..config import settings

# ── Keywords that signal a GitHub write operation ──────────────────────────────
_GITHUB_WRITE_KEYWORDS = {
    "create file", "update file", "delete file", "create repo",
    "delete repo", "push", "commit", "merge", "create pr",
    "open pr", "create pull request", "create branch", "delete branch",
    "fork repo", "rename repo", "archive repo", "add collaborator",
}

# ── Keywords that signal a dangerous shell write operation ────────────────────
_SHELL_WRITE_KEYWORDS = {
    "rm ", "rmdir", "mv ", "sudo ", "chmod", "chown",
    "git push", "git commit", "git merge", "git rebase",
    "git reset --hard", "dd ", "> /", "mkfs",
}


def is_critical_request(message: str) -> bool:
    """Return True if the message is requesting a critical write operation."""
    lower = message.lower()
    return (
        any(k in lower for k in _GITHUB_WRITE_KEYWORDS)
        or any(k in lower for k in _SHELL_WRITE_KEYWORDS)
    )


def has_safe_word(message: str) -> bool:
    """Return True if the owner's safe word appears in the message."""
    word = settings.owner_safe_word
    if not word:
        return True  # No safe word configured — open (dev/local mode only)
    return word in message  # Case-sensitive exact match


def check_authorization(message: str) -> tuple[bool, str]:
    """
    Check whether the message is authorized for critical operations.

    Returns:
        (True, "")       — request is safe OR owner safe word is present
        (False, reason)  — request is critical and safe word is missing
    """
    if not is_critical_request(message):
        return True, ""

    if has_safe_word(message):
        return True, ""

    return False, (
        "This request involves modifying critical systems "
        "(GitHub repositories, n8n workflows, or system files). "
        "For security, only the system owner can authorize these actions. "
        "If you are the owner, please re-send your request with your authorization code."
    )
