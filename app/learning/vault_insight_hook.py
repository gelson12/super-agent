"""
Post-response vault insight hook.

Detects significant conversation exchanges and appends a brief structured
note to Conversations/YYYY-MM-DD.md in the Obsidian vault.

Runs in a daemon thread — never blocks the response path. Never raises.
Called from dispatcher just before return _build_extended_result(...).

Deduplication: per session_id, at most one vault write per 15 minutes.
This prevents rapid-fire conversations from flooding the vault with
near-identical notes.
"""
import datetime
import threading
import time

_AGENT_OUTCOME_PATHS = {
    "N8N":          "KnowledgeBase/n8n/outcomes.md",
    "SHELL":        "KnowledgeBase/Shell/outcomes.md",
    "GITHUB":       "KnowledgeBase/GitHub/outcomes.md",
    "SELF_IMPROVE": "KnowledgeBase/SelfImprove/outcomes.md",
}
_AGENT_COOLDOWN = 300   # 5 min between writes per (agent_type, session_id)
_agent_last_write: dict = {}
_agent_lock = threading.Lock()

_KEYWORDS = {
    "decision":     {"decided", "going to", "will use", "chose", "switched to",
                     "plan to", "agreed", "confirmed"},
    "goal":         {"goal", "objective", "priority", "want to", "need to",
                     "trying to", "building", "working on"},
    "bug":          {"error", "bug", "broken", "failing", "fixed",
                     "resolved", "root cause"},
    "architecture": {"architecture", "deploy", "railway", "docker", "workflow",
                     "agent", "route", "model", "prompt"},
    "preference":   {"prefer", "always", "never", "don't", "instead of",
                     "rather than"},
    "completion":   {"build succeeded", "deployed", "activated", "created workflow",
                     "apk ready", "download link", "completed", "pushed to",
                     "committed", "merged", "workflow activated"},
}

# Auto-tag rules: (keyword_set, tag_string)
_OUTCOME_TAG_RULES: list[tuple[set, str]] = [
    ({"flutter", "apk", "pubspec", "dart", "build apk"},           "build"),
    ({"workflow", "n8n", "automation", "webhook", "trigger"},       "automation"),
    ({"error", "fixed", "resolved", "root cause", "bug", "debug"},  "debug"),
    ({"deploy", "railway", "redeploy", "supervisorctl", "service"}, "infra"),
    ({"github", "commit", "push", "pull request", "repo"},          "github"),
    ({"email", "calendar", "secretary", "outlook"},                 "secretary"),
    ({"database", "postgres", "db_health", "query"},                "database"),
    ({"improvement", "self-improve", "fix your", "patch"},          "self-improve"),
    ({"shell", "bash", "command", "terminal"},                      "shell"),
]


def _detect_outcome_tags(message: str, response: str) -> list[str]:
    """Return content-based tags for an agent outcome note."""
    combined = (message + " " + response).lower()
    return [tag for kws, tag in _OUTCOME_TAG_RULES if any(kw in combined for kw in kws)]
_MIN_RESPONSE_LEN  = 50   # lowered from 200 — captures build completions, short deploy results
_MIN_MSG_LEN       = 10
_SESSION_COOLDOWN  = 900          # 15 minutes between vault writes per session
_session_last_write: dict = {}    # {session_id: epoch_float}
_session_lock = threading.Lock()


def _detect_significance(message: str, response: str) -> list:
    """Return list of matched category names, or [] if not significant."""
    if len(message) < _MIN_MSG_LEN or len(response) < _MIN_RESPONSE_LEN:
        return []
    combined = (message + " " + response).lower()
    matched = []
    for category, keywords in _KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            matched.append(category)
    return matched


def _append_to_vault(path: str, content: str) -> None:
    """Append content to vault file via SSE MCP client. Never raises."""
    try:
        import asyncio
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        from ..tools.obsidian_tools import VAULT_MCP_URL as _URL

        async def _run():
            async with sse_client(url=_URL) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    await s.call_tool(
                        "append_to_file", {"path": path, "content": content}
                    )

        asyncio.run(_run())
    except Exception:
        pass


def _is_session_throttled(session_id: str) -> bool:
    """Return True if this session wrote to the vault within the last 15 minutes."""
    key = session_id or "__global__"
    with _session_lock:
        last = _session_last_write.get(key, 0.0)
        if time.time() - last < _SESSION_COOLDOWN:
            return True
        _session_last_write[key] = time.time()
        # Keep dict bounded — evict entries older than 2x cooldown
        cutoff = time.time() - _SESSION_COOLDOWN * 2
        stale = [k for k, v in _session_last_write.items() if v < cutoff]
        for k in stale:
            del _session_last_write[k]
        return False


def _is_agent_throttled(agent_type: str, session_id: str) -> bool:
    key = (agent_type, session_id or "__global__")
    with _agent_lock:
        last = _agent_last_write.get(key, 0.0)
        if time.time() - last < _AGENT_COOLDOWN:
            return True
        _agent_last_write[key] = time.time()
        cutoff = time.time() - _AGENT_COOLDOWN * 4
        for k in [k for k, v in _agent_last_write.items() if v < cutoff]:
            del _agent_last_write[k]
        return False


def _summarise(text: str, max_len: int = 200) -> str:
    import re
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#+\s*', '', text)
    text = text.strip()
    return text[:max_len].rsplit(' ', 1)[0] + '…' if len(text) > max_len else text


def log_agent_outcome(agent_type: str, message: str, response: str, session_id: str = "") -> None:
    """
    Write a compact structured outcome note for an agent call to its KnowledgeBase path.
    Runs in a daemon thread — never blocks the response path. Throttled per (agent, session).
    """
    if not response or len(response) < 30:
        return
    agent_upper = agent_type.upper()
    path = _AGENT_OUTCOME_PATHS.get(agent_upper)
    if not path or _is_agent_throttled(agent_upper, session_id):
        return

    def _write():
        try:
            now = datetime.datetime.utcnow()
            is_error = response.lstrip().startswith("[") and any(
                k in response.lower() for k in ("error", "failed", "timeout", "unavailable")
            )
            outcome = "ERROR" if is_error else "OK"
            tags = _detect_outcome_tags(message, response)
            tags_line = f"**Tags:** {', '.join(f'#{t}' for t in tags)}\n\n" if tags else ""
            note = (
                f"\n## {now.strftime('%Y-%m-%d %H:%M')} — {outcome}\n\n"
                f"**Task:** {_summarise(message, 180)}\n\n"
                f"**Result:** {_summarise(response, 250)}\n\n"
                f"{tags_line}"
                f"**Session:** {session_id[:16] if session_id else 'n/a'}\n"
            )
            _append_to_vault(path, note)
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()


# ── Auto-pattern extraction ────────────────────────────────────────────────────
# When an agent successfully completes a task, extract the "winning pattern"
# (the command, approach, or sequence that worked) and append it to the agent's
# patterns.md file. This is the mechanism that makes the system actually learn —
# future runs read patterns.md before acting, so they replay what worked.

_AGENT_PATTERNS_PATHS = {
    "SHELL":        "Shell/patterns.md",
    "GITHUB":       "GitHub/patterns.md",
    "N8N":          "n8n/patterns.md",
    "SELF_IMPROVE": "KnowledgeBase/SelfImprove/outcomes.md",  # no patterns.md yet
}

# Markers that indicate a task genuinely completed (not just a response)
_COMPLETION_MARKERS = {
    "build succeeded", "apk ready", "download link", "deployed successfully",
    "workflow activated", "created workflow", "committed", "merged", "pushed to",
    "pull request created", "file created", "file updated", "workflow created",
    "task complete", "done.", "✓", "✅", "successfully", "fixed and deployed",
    "redeployed", "supervisorctl", "activated", "upload complete",
}

# Commands or patterns worth extracting from successful shell/build responses
_COMMAND_PATTERNS = [
    r'`([^`\n]{10,120})`',          # backtick-quoted commands
    r'```(?:bash|sh|python)?\n(.*?)```',  # fenced code blocks (up to 3 lines)
]

# Per-agent, per-category pattern extraction cooldown — 1 per hour max
_pattern_last_write: dict = {}
_pattern_lock = threading.Lock()
_PATTERN_COOLDOWN = 3600  # 1 hour


def _is_pattern_throttled(agent_type: str, category: str) -> bool:
    key = (agent_type, category)
    with _pattern_lock:
        last = _pattern_last_write.get(key, 0.0)
        if time.time() - last < _PATTERN_COOLDOWN:
            return True
        _pattern_last_write[key] = time.time()
        return False


def _is_successful_completion(response: str) -> bool:
    """Return True if response contains genuine task-completion signals."""
    lower = response.lower()
    # Error responses never count
    if response.lstrip().startswith("[") and any(
        k in lower for k in ("error", "failed", "timeout", "unavailable", "refused")
    ):
        return False
    return any(marker in lower for marker in _COMPLETION_MARKERS)


def _extract_key_commands(response: str) -> list[str]:
    """Pull up to 3 commands/code snippets from a successful response."""
    import re
    found = []
    for pat in _COMMAND_PATTERNS:
        for m in re.finditer(pat, response, re.DOTALL):
            snippet = m.group(1).strip()
            lines = [l.strip() for l in snippet.splitlines() if l.strip()]
            if lines:
                found.append(lines[0][:120])  # first line only, capped
            if len(found) >= 3:
                break
        if len(found) >= 3:
            break
    return found[:3]


def extract_and_write_pattern(
    agent_type: str,
    message: str,
    response: str,
    session_id: str = "",
) -> None:
    """
    After a successful agent completion, extract the winning pattern and append
    it to the agent's patterns.md file so future runs can replay what worked.

    This is fire-and-forget — runs in a daemon thread, never blocks.
    Throttled to at most 1 write per agent-category per hour to avoid noise.
    """
    if not _is_successful_completion(response):
        return

    agent_upper = agent_type.upper()
    patterns_path = _AGENT_PATTERNS_PATHS.get(agent_upper)
    if not patterns_path:
        return

    # Detect category from outcome tags (reuse existing tag rules)
    tags = _detect_outcome_tags(message, response)
    category = tags[0] if tags else "general"

    if _is_pattern_throttled(agent_upper, category):
        return

    def _write():
        try:
            now = datetime.datetime.utcnow()
            commands = _extract_key_commands(response)
            cmd_block = ""
            if commands:
                cmd_block = "\n".join(f"  - `{c}`" for c in commands) + "\n"

            pattern_entry = (
                f"\n### [{now.strftime('%Y-%m-%d')}] {_summarise(message, 100)} "
                f"#{category}\n"
                f"**What worked:** {_summarise(response, 200)}\n"
            )
            if cmd_block:
                pattern_entry += f"**Commands:**\n{cmd_block}"

            _append_to_vault(patterns_path, pattern_entry)
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()


def maybe_save_insight(
    message: str,
    response: str,
    model_used: str,
    session_id: str = "",
) -> None:
    """
    Check if this exchange is significant; if so, append a vault note in a
    daemon thread. Returns immediately — never blocks.

    Per-session 15-minute cooldown prevents rapid-fire conversations from
    writing many near-identical notes.
    """
    tags = _detect_significance(message, response)
    if not tags:
        return

    if _is_session_throttled(session_id):
        return

    def _write():
        try:
            now = datetime.datetime.utcnow()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M")
            path = f"Conversations/{date_str}.md"
            note = (
                f"\n## {time_str} — {model_used}\n\n"
                f"**Q:** {message[:300]}\n\n"
                f"**A:** {response[:400]}\n\n"
                f"**Tags:** {', '.join(tags)}\n"
            )
            _append_to_vault(path, note)
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()
