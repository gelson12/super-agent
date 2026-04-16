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


@tool
def obsidian_list_folders() -> str:
    """
    List all folder names in the Obsidian vault (no files, folders only).
    Use this to navigate the vault structure before reading or writing notes.
    Returns a newline-separated list of folder paths, plus '(root)' if notes
    exist at the top level.
    """
    return _run_async(_call_mcp("list_folders", {}))


@tool
def obsidian_get_recent_notes(n: int = 10) -> str:
    """
    Return the N most recently modified notes in the vault with timestamps.
    Default is 10. Use this to catch up on recent activity or find the latest
    improvement logs, conversation notes, or engineering decisions.
    """
    return _run_async(_call_mcp("get_recent_notes", {"n": n}))


@tool
def obsidian_get_vault_summary() -> str:
    """
    Return a full digest of all vault notes grouped by folder, showing note
    names and approximate word counts. Use this to get a bird's-eye view of
    everything stored in the vault before deciding which notes to read.
    """
    return _run_async(_call_mcp("get_vault_summary", {}))


@tool
def obsidian_move_note(from_path: str, to_path: str) -> str:
    """
    Move or rename a note in the Obsidian vault.
    - from_path: current relative path (e.g. "Inbox/draft.md")
    - to_path:   new relative path   (e.g. "Engineering/architecture.md")
    Creates intermediate folders automatically. The original file is removed.
    """
    return _run_async(_call_mcp("move_file", {"from_path": from_path, "to_path": to_path}))


@tool
def obsidian_search_by_tag(tag: str) -> str:
    """
    Find all notes in the vault that contain a given tag.
    Matches both YAML frontmatter tags (tags: [foo]) and inline #hashtags.
    Pass the tag without the # prefix (e.g. "architecture" not "#architecture").
    Returns a newline-separated list of matching file paths.
    """
    return _run_async(_call_mcp("search_by_tag", {"tag": tag}))


# ── Convenience list for agent tool registration ──────────────────────────────

@tool
def obsidian_get_note_metadata(file_path: str) -> str:
    """
    Read YAML frontmatter metadata from a vault note without loading the full content.
    Returns a JSON object with frontmatter fields (tags, date, type, etc.).
    Use this for fast tag/date scanning without reading thousands of characters.
    """
    return _run_async(_call_mcp("get_note_metadata", {"path": file_path}))


@tool
def obsidian_archive_old_notes(days: int = 90, dry_run: bool = False) -> str:
    """
    Move notes older than N days into Archive/YYYY-MM/ folders to keep the
    active vault clean without deleting history.
    - days: age threshold (default 90). Notes last modified before this are archived.
    - dry_run: if True, reports what would be moved without actually moving anything.
    Returns a summary of archived and kept note counts.
    Always call with dry_run=True first to preview before actually archiving.
    """
    return _run_async(_call_mcp("archive_old_notes", {"days": days, "dry_run": dry_run}))


# ── Convenience list for agent tool registration ──────────────────────────────

OBSIDIAN_TOOLS = [
    obsidian_list_notes,
    obsidian_read_note,
    obsidian_write_note,
    obsidian_append_to_note,
    obsidian_search_vault,
    obsidian_discover_tools,
    obsidian_call_tool,
    obsidian_list_folders,
    obsidian_get_recent_notes,
    obsidian_get_vault_summary,
    obsidian_move_note,
    obsidian_search_by_tag,
    obsidian_get_note_metadata,
    obsidian_archive_old_notes,
]
