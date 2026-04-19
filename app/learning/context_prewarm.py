"""
Predictive context pre-warmer.

After each agent response, fires a daemon thread to pre-warm the vault patterns
cache for the predicted next agent — so the next request hits a warm cache
instead of paying the MCP round-trip latency at request time.

Zero API cost — only touches the internal Obsidian vault MCP server.
"""
import threading
import time


def prewarm_for_agent(agent_key: str) -> None:
    """
    Pre-warm vault patterns for `agent_key` in a background daemon thread.
    agent_key must be one of: "n8n", "shell", "github", "self_improve".
    No-ops immediately if the cache is already warm.
    """
    if not agent_key or agent_key not in ("n8n", "shell", "github", "self_improve"):
        return
    threading.Thread(target=_do_prewarm, args=(agent_key,), daemon=True).start()


def _do_prewarm(agent_key: str) -> None:
    """Fetch and populate patterns cache for the given agent. Never raises."""
    try:
        from ..prompts import _patterns_cache, _PATTERNS_TTL, _AGENT_PATTERNS_FILES

        # Skip if cache is already warm
        _, cached_ts = _patterns_cache.get(agent_key, ("", 0.0))
        if time.time() - cached_ts < _PATTERNS_TTL:
            return

        path = _AGENT_PATTERNS_FILES.get(agent_key, "")
        if not path:
            return

        import asyncio
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        from ..tools.obsidian_tools import VAULT_MCP_URL as _URL

        async def _fetch():
            async with sse_client(url=_URL) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool("read_file", {"path": path})
                    return res.content[0].text if res.content else ""

        text = asyncio.run(_fetch())
        if text and "not found" not in text.lower():
            _patterns_cache[agent_key] = (text, time.time())
    except Exception:
        pass
