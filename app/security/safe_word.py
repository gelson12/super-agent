"""
Safe word authorization guard.

Critical operations (GitHub writes, shell write commands, n8n workflow
create/update/delete) are blocked unless the owner's safe word is present
in the request message.

The safe word is stored in the OWNER_SAFE_WORD environment variable.
Default: alpha0  (change via Railway env var — do NOT commit the real word).

Session whitelist: pre-trusted session IDs (e.g., the Bridge executive
super-agent bots, whose system prompts legitimately describe destructive
operations they never request) can bypass the guard. The default pattern
matches `bridge-<bot>-<timestamp>` session IDs emitted by
super-agent/scripts/_bridge_bot_skeleton.py. Additional patterns may be
added via the SAFE_WORD_SESSION_WHITELIST env var (comma-separated regex).
"""
import os
import re
import logging
import threading
import time
import unicodedata

from ..config import settings

_log = logging.getLogger("safe_word")

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
    r"\bactivate\b.*\bworkflow\b",
    r"\bdeactivate\b.*\bworkflow\b",
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


def _normalize(text: str) -> str:
    """NFKC-normalize to collapse Unicode homoglyphs (Cyrillic 'а' → Latin 'a', etc.)."""
    return unicodedata.normalize("NFKC", text).lower()


def has_safe_word(message: str) -> bool:
    """Return True if the owner's safe word appears in the message.
    Uses NFKC normalization to defeat Unicode homoglyph bypass attempts."""
    word = settings.owner_safe_word
    if not word:
        return True  # No safe word configured — open (dev/local mode only)
    return _normalize(word) in _normalize(message)


def _detect_operation_type(message: str) -> str:
    """
    Detect what kind of critical operation was requested.
    Returns a human-readable label for the block message.
    """
    lower = message.lower()
    # Check n8n workflow patterns
    _workflow_verbs = ("create", "build", "make", "add", "delete", "remove",
                       "destroy", "update", "modify", "edit", "activate", "deactivate")
    if any(v in lower for v in _workflow_verbs) and "workflow" in lower:
        if any(v in lower for v in ("activate", "deactivate")):
            verb = "activate" if "activate" in lower else "deactivate"
            return f"n8n workflow {verb}"
        if any(v in lower for v in ("delete", "remove", "destroy")):
            return "n8n workflow deletion"
        return "n8n workflow creation/modification"
    # Shell commands
    _shell_ops = ("rm ", "rmdir", "sudo ", "chmod", "chown", "dd ", "> /", "mkfs")
    if any(op in lower for op in _shell_ops):
        return "shell command (destructive)"
    # Git operations
    _git_ops = ("git push", "git commit", "git merge", "git rebase", "git reset --hard")
    if any(op in lower for op in _git_ops):
        return "git operation"
    # GitHub operations
    _github_ops = ("pull request", " pr", "branch", "repo", "collaborator", "fork")
    if any(op in lower for op in _github_ops):
        return "GitHub repository operation"
    # Generic file write
    if any(v in lower for v in ("create file", "update file", "delete file")):
        return "file write operation"
    return "critical system operation"


# ── Session-ID whitelist (pre-trusted sessions skip the guard) ───────────────
# Bridge executive super-agent bots use session IDs of the form
# `bridge-<bot>-<YYYYMMDD-HHmm>` (see super-agent/scripts/_bridge_bot_skeleton.py
# code_assemble_prompt). Their system prompts legitimately describe destructive
# operations (Cleaner -> "delete demo websites", CSO -> "credentials may be
# exposed") which would otherwise trigger the safe-word block. They never
# actually request execution of those operations — the workflow's risk-tagged
# action dispatcher gates that separately.
_DEFAULT_WHITELIST_PATTERNS = [
    r"^bridge-(researcher|chief_of_staff|cso|ceo|cleaner|programmer|pm|finance|marketing|website|cro)-",
]


def _build_whitelist() -> list[re.Pattern]:
    """Compile the default whitelist plus any patterns from SAFE_WORD_SESSION_WHITELIST."""
    extra = os.environ.get("SAFE_WORD_SESSION_WHITELIST", "").strip()
    patterns = list(_DEFAULT_WHITELIST_PATTERNS)
    if extra:
        patterns.extend(p.strip() for p in extra.split(",") if p.strip())
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error as e:
            _log.warning("Skipping invalid SAFE_WORD_SESSION_WHITELIST pattern %r: %s", p, e)
    return compiled


_WHITELIST_COMPILED = _build_whitelist()


def is_whitelisted_session(session_id: str | None) -> bool:
    """Return True if the session_id matches any whitelist pattern."""
    if not session_id:
        return False
    return any(pat.search(session_id) for pat in _WHITELIST_COMPILED)


# ── Pending-approval store ────────────────────────────────────────────────────
# When a critical request is blocked, we stash it here keyed by session_id.
# On the NEXT message from the same session, if it contains the safe word,
# we auto-approve the stashed request instead of making the user retype everything.
# TTL: 30 minutes — stale entries are ignored and evicted on next access.
_PENDING_TTL = 1800  # 30 minutes
_pending: dict[str, tuple[float, str]] = {}  # session_id → (ts, original_message)
_pending_lock = threading.Lock()


def store_pending(session_id: str, message: str) -> None:
    """Stash a blocked message for potential safe-word re-approval."""
    if not session_id:
        return
    with _pending_lock:
        _pending[session_id] = (time.time(), message)


def pop_pending(session_id: str) -> str | None:
    """Return and remove the pending blocked message for this session (if still fresh)."""
    if not session_id:
        return None
    with _pending_lock:
        entry = _pending.pop(session_id, None)
    if entry and time.time() - entry[0] < _PENDING_TTL:
        return entry[1]
    return None


def has_pending(session_id: str) -> bool:
    """True if there is a non-expired pending blocked request for this session."""
    if not session_id:
        return False
    with _pending_lock:
        entry = _pending.get(session_id)
    return bool(entry and time.time() - entry[0] < _PENDING_TTL)


def check_authorization(message: str, session_id: str | None = None) -> tuple[bool, str]:
    """
    Check whether the message is authorized for critical operations.

    Args:
        message:    the user's input.
        session_id: optional session identifier; sessions matching the
                    whitelist (Bridge executive bots, plus any patterns
                    in SAFE_WORD_SESSION_WHITELIST) bypass the guard.

    Returns:
        (True, "")       — request is safe, owner safe word is present,
                           OR session is pre-trusted
        (False, reason)  — request is critical and safe word is missing
    """
    if not is_critical_request(message):
        return True, ""

    if has_safe_word(message):
        return True, ""

    if is_whitelisted_session(session_id):
        return True, ""

    op_type = _detect_operation_type(message)
    try:
        from ..activity_log import bg_log as _bg_log
        _bg_log(
            f"SAFE WORD BLOCK: session attempted critical op without auth: {message[:120]}",
            source="safe_word",
        )
    except Exception:
        _log.warning("SAFE WORD BLOCK: critical op attempted without auth: %.120s", message)

    # Stash the blocked request so the owner can re-approve by just sending
    # the safe word (no need to retype the full message).
    store_pending(session_id or "default", message)

    word = settings.owner_safe_word
    hint = f' Include the phrase `{word}` anywhere in your reply.' if word else ""

    return False, (
        f"⚠️ **Authorization required** — this request involves a **{op_type}**.\n\n"
        "Only the system owner can authorize this action.\n\n"
        f"**To approve:** reply with your authorization code and I will re-run the request automatically.{hint}\n\n"
        f"**Request held for 30 minutes** — you do not need to retype it.\n\n"
        "_If you did not send this request, it may have come from a scheduled workflow. "
        "Check the activity log at `/activity` to investigate._"
    )
