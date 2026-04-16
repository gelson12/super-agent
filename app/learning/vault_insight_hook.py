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
        _URL = "http://obsidian-vault.railway.internal:22360/sse"

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
