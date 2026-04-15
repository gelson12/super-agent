"""
Safe word authorization guard.

Critical operations (GitHub writes, shell write commands, n8n workflow
create/update/delete) are blocked unless the owner's safe word is present
in the request message.

The safe word is stored in the OWNER_SAFE_WORD environment variable.
Default: alpha0  (change via Railway env var — do NOT commit the real word).
"""
import re

from ..config import settings

# ── GitHub write: verb + object pairs (matched with flexible word gaps) ───────
# These match "create file", "create a file", "create the file", etc.
_GITHUB_WRITE_PATTERNS = [
    r"\bcreate\b.*\bfile\b",
    r"\bupdate\b.*\bfile\b",
    r"\bdelete\b.*\bfile\b",
    r"\bcreate\b.*\brepo\b",
    r"\bdelete\b.*\brepo\b",
    r"\bcreate\b.*\bpull request\b",
    r"\bcreate\b.*\bbranch\b",
    r"\bdelete\b.*\bbranch\b",
    r"\bcreate\b.*\bpr\b",
    r"\bopen\b.*\bpr\b",
    r"\bfork\b.*\brepo\b",
    r"\brename\b.*\brepo\b",
    r"\barchive\b.*\brepo\b",
    r"\badd\b.*\bcollaborator\b",
    # ── n8n workflow write operations ─────────────────────────────────────────
    r"\bcreate\b.*\bworkflow\b",
    r"\bbuild\b.*\bworkflow\b",
    r"\bmake\b.*\bworkflow\b",
    r"\badd\b.*\bworkflow\b",
    r"\bdelete\b.*\bworkflow\b",
    r"\bremove\b.*\bworkflow\b",
    r"\bdestroy\b.*\bworkflow\b",
    r"\bupdate\b.*\bworkflow\b",
    r"\bmodify\b.*\bworkflow\b",
    r"\bedit\b.*\bworkflow\b",
]

# ── Exact substring matches (shell commands, git operations) ─────────────────
_EXACT_WRITE_KEYWORDS = {
    "push", "commit", "merge",
    "rm ", "rmdir", "mv ", "sudo ", "chmod", "chown",
    "git push", "git commit", "git merge", "git rebase",
    "git reset --hard", "dd ", "> /", "mkfs",
}

# Pre-compile patterns for performance
_GITHUB_COMPILED = [re.compile(p, re.IGNORECASE) for p in _GITHUB_WRITE_PATTERNS]


def is_critical_request(message: str) -> bool:
    """Return True if the message is requesting a critical write operation."""
    lower = message.lower()
    # Check regex patterns (flexible matching for GitHub writes)
    if any(pat.search(lower) for pat in _GITHUB_COMPILED):
        return True
    # Check exact substrings (shell commands)
    return any(k in lower for k in _EXACT_WRITE_KEYWORDS)


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
