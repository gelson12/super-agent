"""
Obsidian Vault Tools — gives LangGraph agents (Haiku, Sonnet, Opus) access to
the Obsidian knowledge vault running in the obsidian-vault Railway service.

The obsidian-vault service runs Obsidian headlessly (Xvfb) with the
"Claude Code MCP" plugin by Ian Sinnott, which exposes an MCP WebSocket
server on port 22360.

Connection:
  - Railway containers: ws://obsidian-vault.railway.internal:22360  (default)
  - Local sessions:     ws://localhost:22360  (if Obsidian is open locally)

Override with env var: OBSIDIAN_MCP_URL

All tools degrade gracefully — if the vault is unavailable, they return an
informative message instead of raising an exception. Agents should treat an
unavailability response as a soft failure and continue without vault context.
"""

import asyncio
import json
import logging
import os
import concurrent.futures
from langchain.tools import tool

logger = logging.getLogger(__name__)

_OBSIDIAN_URL = os.environ.get("OBSIDIAN_MCP_URL", "http://obsidian-vault.railway.internal:22360/sse")


# ── Async MCP WebSocket client ────────────────────────────────────────────────

async def _call_mcp(tool_name: str, arguments: dict) -> str:
    """
    Open a fresh MCP session over SSE/HTTP, call one tool, return the text result.
    """
    try:
        from mcp.client.sse import sse_client
        from mcp import ClientSession

        async with sse_client(_OBSIDIAN_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                texts = [
                    c.text for c in result.content
                    if hasattr(c, "text") and c.text
                ]
                return "\n".join(texts) if texts else "(empty response from vault)"

    except Exception as exc:
        logger.debug("Obsidian MCP call failed: %s", exc)
        return f"[obsidian vault unavailable — {type(exc).__name__}: {exc}]"


async def _list_mcp_tools() -> list[dict]:
    """Discover what tools the Obsidian MCP server actually exposes."""
    try:
        from mcp.client.sse import sse_client
        from mcp import ClientSession

        async with sse_client(_OBSIDIAN_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [{"name": t.name, "description": t.description} for t in result.tools]
    except Exception as exc:
        return [{"error": str(exc)}]


def _run_async(coro, timeout: int = 20) -> str:
    """
    Safely run an async coroutine from a synchronous LangChain tool.
    Handles both cases: no running event loop (simple asyncio.run) and
    already-running loop inside LangGraph (thread pool executor).
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # LangGraph runs inside an event loop — use a thread to avoid deadlock
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=timeout)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
    except concurrent.futures.TimeoutError:
        return "[obsidian vault timed out — service may be starting up, retry in ~15s]"


# ── LangChain tools ───────────────────────────────────────────────────────────

@tool
def obsidian_list_notes(directory: str = "") -> str:
    """
    List notes (markdown files) in the Obsidian vault.
    Optionally pass a subdirectory path to list only files in that folder.
    Returns a newline-separated list of file paths.
    Use this to discover what knowledge is stored in the vault before reading.
    """
    args = {"path": directory} if directory else {}
    return _run_async(_call_mcp("list_directory", args))


@tool
def obsidian_read_note(file_path: str) -> str:
    """
    Read the full content of a note from the Obsidian vault.
    Pass the relative file path as returned by obsidian_list_notes
    (e.g. "Self-Improvement Ideas.md" or "context/architecture.md").
    Use this to read existing knowledge, improvement ideas, or saved context.
    """
    return _run_async(_call_mcp("read_file", {"path": file_path}))


@tool
def obsidian_write_note(file_path: str, content: str) -> str:
    """
    Create or overwrite a note in the Obsidian vault.
    - file_path: relative path including .md extension (e.g. "improvements/2026-04-14.md")
    - content: full markdown content to write
    Use this to record self-improvement insights, save architecture decisions,
    log improvement reports, or store any knowledge for future reference.
    Overwriting an existing file replaces its content entirely.
    """
    return _run_async(_call_mcp("write_file", {"path": file_path, "content": content}))


@tool
def obsidian_append_to_note(file_path: str, content: str) -> str:
    """
    Append text to an existing note in the Obsidian vault without overwriting it.
    - file_path: relative path to the note (e.g. "improvements/log.md")
    - content: markdown text to append at the end of the file
    Use this to add new entries to ongoing logs, improvement diaries, or running lists.
    Creates the file if it does not exist.
    """
    return _run_async(_call_mcp("append_to_file", {"path": file_path, "content": content}))


@tool
def obsidian_search_vault(query: str) -> str:
    """
    Full-text search across all notes in the Obsidian vault.
    Returns matching file paths and relevant excerpts.
    Use this to find prior improvement ideas, past decisions, architecture notes,
    or any previously saved knowledge relevant to the current task.
    """
    return _run_async(_call_mcp("search_files", {"query": query}))


@tool
def obsidian_discover_tools() -> str:
    """
    List all tools currently available from the Obsidian MCP server.
    Use this if other obsidian tools fail — it reveals the actual tool names
    the plugin exposes so you can call them directly via obsidian_call_tool.
    Also useful to verify the vault connection is healthy.
    """
    result = _run_async(_list_mcp_tools(), timeout=10)
    if isinstance(result, list):
        return json.dumps(result, indent=2)
    return str(result)


@tool
def obsidian_call_tool(tool_name: str, arguments_json: str = "{}") -> str:
    """
    Call any tool on the Obsidian MCP server by name with arbitrary arguments.
    Use this as an escape hatch when the specific obsidian_* tools don't match
    the actual tool names exposed by the plugin.
    - tool_name: exact MCP tool name (find via obsidian_discover_tools)
    - arguments_json: JSON string of arguments (e.g. '{"path": "notes/foo.md"}')
    """
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"[invalid arguments_json: {e}]"
    return _run_async(_call_mcp(tool_name, args))


# ── Convenience list for agent tool registration ──────────────────────────────

OBSIDIAN_TOOLS = [
    obsidian_list_notes,
    obsidian_read_note,
    obsidian_write_note,
    obsidian_append_to_note,
    obsidian_search_vault,
    obsidian_discover_tools,
    obsidian_call_tool,
]
