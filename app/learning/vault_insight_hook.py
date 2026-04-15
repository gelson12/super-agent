"""
Post-response vault insight hook.

Detects significant conversation exchanges and appends a brief structured
note to Conversations/YYYY-MM-DD.md in the Obsidian vault.

Runs in a daemon thread — never blocks the response path. Never raises.
Called from dispatcher just before return _build_extended_result(...).
"""
import datetime
import threading

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
}
_MIN_RESPONSE_LEN = 200
_MIN_MSG_LEN      = 15


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


def maybe_save_insight(
    message: str,
    response: str,
    model_used: str,
    session_id: str = "",
) -> None:
    """
    Check if this exchange is significant; if so, append a vault note in a
    daemon thread. Returns immediately — never blocks.
    """
    tags = _detect_significance(message, response)
    if not tags:
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
