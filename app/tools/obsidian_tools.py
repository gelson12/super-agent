"""
Daphney Tools — gives LangGraph agents (Haiku, Sonnet, Opus) access to
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
import threading
import concurrent.futures
from langchain.tools import tool

logger = logging.getLogger(__name__)

# Single source of truth for the vault MCP URL.
# Override in Railway: set OBSIDIAN_MCP_URL env var.
VAULT_MCP_URL = os.environ.get("OBSIDIAN_MCP_URL", "http://obsidian-vault.railway.internal:22360/sse")
_OBSIDIAN_URL = VAULT_MCP_URL  # internal alias kept for backward compat

# ── Calling-agent context (set by agent dispatch layer) ──────────────────────
_agent_ctx = threading.local()


def set_calling_agent(agent_name: str) -> None:
    """Set which agent is currently running, so vault tools can draw the right talking line."""
    _agent_ctx.name = agent_name


def _get_calling_agent() -> str:
    return getattr(_agent_ctx, "name", "Self-Improve Agent")


def _vault_enter(tool_name: str) -> str:
    """Mark vault as active and show talking line to the calling agent."""
    caller = _get_calling_agent()
    try:
        from ..learning.agent_status_tracker import mark_working, mark_talking
        mark_working("Daphney", tool_name)
        mark_talking(caller, "Daphney")
    except Exception:
        pass
    return caller


def _vault_exit(caller: str) -> None:
    """Mark vault as done and clear talking line."""
    try:
        from ..learning.agent_status_tracker import mark_done, clear_talking
        clear_talking(caller, "Daphney")
        mark_done("Daphney")
    except Exception:
        pass


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


def _run_vault_tool(tool_name: str, coro, timeout: int = 20) -> str:
    """Run a vault MCP call with dashboard tracking (working state + talking lines)."""
    caller = _vault_enter(tool_name)
    try:
        return _run_async(coro, timeout=timeout)
    finally:
        _vault_exit(caller)


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
    return _run_vault_tool("list_notes", _call_mcp("list_directory", args))


@tool
def obsidian_read_note(file_path: str) -> str:
    """
    Read the full content of a note from the Obsidian vault.
    Pass the relative file path as returned by obsidian_list_notes
    (e.g. "Self-Improvement Ideas.md" or "context/architecture.md").
    Use this to read existing knowledge, improvement ideas, or saved context.
    """
    return _run_vault_tool("read_note", _call_mcp("read_file", {"path": file_path}))


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
    return _run_vault_tool("write_note", _call_mcp("write_file", {"path": file_path, "content": content}))


@tool
def obsidian_append_to_note(file_path: str, content: str) -> str:
    """
    Append text to an existing note in the Obsidian vault without overwriting it.
    - file_path: relative path to the note (e.g. "improvements/log.md")
    - content: markdown text to append at the end of the file
    Use this to add new entries to ongoing logs, improvement diaries, or running lists.
    Creates the file if it does not exist.
    """
    return _run_vault_tool("append_note", _call_mcp("append_to_file", {"path": file_path, "content": content}))


@tool
def obsidian_search_vault(query: str) -> str:
    """
    Full-text search across all notes in the Obsidian vault.
    Returns matching file paths and relevant excerpts.
    Use this to find prior improvement ideas, past decisions, architecture notes,
    or any previously saved knowledge relevant to the current task.
    """
    return _run_vault_tool("search_vault", _call_mcp("search_files", {"query": query}))


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
    except json.JSONDecodeError as e:  # noqa: F841
        return f"[invalid arguments_json: {e}]"
    return _run_vault_tool(tool_name, _call_mcp(tool_name, args))


@tool
def obsidian_list_folders() -> str:
    """
    List all folder names in the Obsidian vault (no files, folders only).
    Use this to navigate the vault structure before reading or writing notes.
    Returns a newline-separated list of folder paths, plus '(root)' if notes
    exist at the top level.
    """
    return _run_vault_tool("list_folders", _call_mcp("list_folders", {}))


@tool
def obsidian_get_recent_notes(n: int = 10) -> str:
    """
    Return the N most recently modified notes in the vault with timestamps.
    Default is 10. Use this to catch up on recent activity or find the latest
    improvement logs, conversation notes, or engineering decisions.
    """
    return _run_vault_tool("get_recent_notes", _call_mcp("get_recent_notes", {"n": n}))


@tool
def obsidian_get_vault_summary() -> str:
    """
    Return a full digest of all vault notes grouped by folder, showing note
    names and approximate word counts. Use this to get a bird's-eye view of
    everything stored in the vault before deciding which notes to read.
    """
    return _run_vault_tool("get_vault_summary", _call_mcp("get_vault_summary", {}))


@tool
def obsidian_move_note(from_path: str, to_path: str) -> str:
    """
    Move or rename a note in the Obsidian vault.
    - from_path: current relative path (e.g. "Inbox/draft.md")
    - to_path:   new relative path   (e.g. "Engineering/architecture.md")
    Creates intermediate folders automatically. The original file is removed.
    """
    return _run_vault_tool("move_note", _call_mcp("move_file", {"from_path": from_path, "to_path": to_path}))


@tool
def obsidian_search_by_tag(tag: str) -> str:
    """
    Find all notes in the vault that contain a given tag.
    Matches both YAML frontmatter tags (tags: [foo]) and inline #hashtags.
    Pass the tag without the # prefix (e.g. "architecture" not "#architecture").
    Returns a newline-separated list of matching file paths.
    """
    return _run_vault_tool("search_by_tag", _call_mcp("search_by_tag", {"tag": tag}))


# ── Convenience list for agent tool registration ──────────────────────────────

@tool
def obsidian_get_note_metadata(file_path: str) -> str:
    """
    Read YAML frontmatter metadata from a vault note without loading the full content.
    Returns a JSON object with frontmatter fields (tags, date, type, etc.).
    Use this for fast tag/date scanning without reading thousands of characters.
    """
    return _run_vault_tool("get_note_metadata", _call_mcp("get_note_metadata", {"path": file_path}))


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
    return _run_vault_tool("archive_old_notes", _call_mcp("archive_old_notes", {"days": days, "dry_run": dry_run}))


@tool
def obsidian_update_frontmatter(file_path: str, fields_json: str, merge: bool = True) -> str:
    """
    Patch YAML frontmatter fields on a vault note without touching the body content.
    - file_path: relative path to the note
    - fields_json: JSON string of key/value pairs to set (e.g. '{"type":"decision","status":"approved"}')
    - merge: if True (default), merges into existing frontmatter; if False, replaces entirely
    Use this to add tags, set a date, mark a note type, or update any metadata property.
    Creates frontmatter if the note has none. Creates the note if it doesn't exist.
    """
    try:
        fields = json.loads(fields_json)
    except Exception as e:
        return f"[invalid fields_json: {e}]"
    return _run_vault_tool("update_frontmatter", _call_mcp("update_note_frontmatter", {"path": file_path, "fields": fields, "merge": merge}))


@tool
def obsidian_get_backlinks(file_path: str) -> str:
    """
    Find all notes in the vault that link to the given note via [[wikilink]] or markdown link.
    Returns a JSON object with the list of referencing file paths and a count.
    Use this to understand the knowledge graph — which notes reference a decision, architecture doc, etc.
    Essential before renaming or deleting a note to understand its impact.
    """
    return _run_vault_tool("get_backlinks", _call_mcp("get_backlinks", {"path": file_path}))


@tool
def obsidian_search_with_filters(
    content_query: str = "",
    tag: str = "",
    path_prefix: str = "",
    frontmatter_key: str = "",
    frontmatter_value: str = "",
) -> str:
    """
    Advanced vault search combining multiple filters. All parameters are optional — combine as needed.
    - content_query: regex or keyword to match in note body
    - tag: filter to notes containing this tag (frontmatter or inline #tag)
    - path_prefix: only search notes under this folder (e.g. "Decisions/")
    - frontmatter_key: only notes where this YAML key exists
    - frontmatter_value: combined with frontmatter_key — only notes where key=value
    Example: find all approved decisions → tag="decision", frontmatter_key="status", frontmatter_value="approved"
    """
    return _run_vault_tool("search_with_filters", _call_mcp("search_with_filters", {
        "content_query": content_query,
        "tag": tag,
        "path_prefix": path_prefix,
        "frontmatter_key": frontmatter_key,
        "frontmatter_value": frontmatter_value,
    }))


@tool
def obsidian_rename_note(from_path: str, to_path: str) -> str:
    """
    Rename or move a note AND automatically update every [[wikilink]] reference to it
    across the entire vault. Unlike obsidian_move_note, this is safe to use on notes
    that other notes link to — it keeps the knowledge graph intact.
    - from_path: current relative path (e.g. "Decisions/old-name.md")
    - to_path:   new relative path   (e.g. "Decisions/new-name.md")
    Returns a summary of the move and how many files had their backlinks updated.
    """
    return _run_vault_tool("rename_note", _call_mcp("rename_note_with_backlink_update", {"from_path": from_path, "to_path": to_path}))


@tool
def obsidian_create_from_template(template_path: str, new_note_path: str, variables_json: str = "{}") -> str:
    """
    Create a new note from a template file stored in the vault's _templates/ folder.
    Substitutes {{variable}} placeholders with provided values.
    Built-in variables always available: {{date}}, {{time}}, {{title}}
    - template_path: relative path to template (e.g. "_templates/decision.md")
    - new_note_path: where to create the new note (e.g. "Decisions/2026-04-17-use-postgresql.md")
    - variables_json: JSON string of variable substitutions (e.g. '{"author":"Super Agent","status":"proposed"}')
    Use this to create consistently structured notes for decisions, architecture docs, or improvement logs.
    """
    try:
        variables = json.loads(variables_json)
    except Exception:
        variables = {}
    return _run_vault_tool("create_from_template", _call_mcp("create_note_from_template", {
        "template_path": template_path,
        "new_note_path": new_note_path,
        "variables": variables,
    }))


@tool
def obsidian_get_all_tags() -> str:
    """
    List every tag used across the entire vault with note counts, sorted by frequency.
    Returns JSON with {total_unique_tags, tags: {tag_name: count}}.
    Use this to understand how the vault is categorised, find related notes by tag cluster,
    or audit the tag taxonomy before doing bulk operations.
    """
    return _run_vault_tool("get_all_tags", _call_mcp("get_all_tags", {}))


@tool
def obsidian_rename_tag(old_tag: str, new_tag: str) -> str:
    """
    Rename a tag across every note in the vault — updates both YAML frontmatter tag lists
    and inline #hashtags. Pass tag names without the # prefix.
    Example: rename "infra" to "infrastructure" everywhere.
    Returns a count of notes updated and their file paths.
    """
    return _run_vault_tool("rename_tag", _call_mcp("rename_tag_everywhere", {"old_tag": old_tag, "new_tag": new_tag}))


@tool
def obsidian_bulk_move(pattern: str, destination_folder: str) -> str:
    """
    Move all notes matching a glob pattern to a destination folder.
    - pattern: glob relative to vault root (e.g. "Inbox/*.md", "Conversations/2025-*.md")
    - destination_folder: target folder path (e.g. "Archive/2025", "Processed")
    Creates the destination folder if it doesn't exist. Skips files that already exist at destination.
    Returns a count and list of moved files. Use for bulk vault reorganisation.
    """
    return _run_vault_tool("bulk_move", _call_mcp("bulk_move_files", {"pattern": pattern, "destination_folder": destination_folder}))


@tool
def obsidian_vault_analytics() -> str:
    """
    Run a full vault health analysis and return:
    - orphaned_notes: notes with no incoming OR outgoing links (isolated, easy to lose)
    - dead_links: [[wikilinks]] pointing to notes that don't exist
    - avg_outgoing_links_per_note: link density metric
    - notes_per_folder: breakdown of how many notes are in each folder
    Use this periodically to keep the vault healthy and discover forgotten notes.
    """
    return _run_vault_tool("vault_analytics", _call_mcp("get_vault_analytics", {}))


@tool
def obsidian_get_note_links(file_path: str) -> str:
    """
    Extract all outgoing links from a note:
    - wikilinks: [[Note Name]] internal references
    - internal_markdown_links: [text](./relative.md) links
    - external_urls: [text](https://...) links
    Returns structured JSON with all link types and a total count.
    Use this to understand a note's connections before editing or to build a local graph view.
    """
    return _run_vault_tool("get_note_links", _call_mcp("get_note_links", {"path": file_path}))


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
    # ── New tools ──
    obsidian_update_frontmatter,
    obsidian_get_backlinks,
    obsidian_search_with_filters,
    obsidian_rename_note,
    obsidian_create_from_template,
    obsidian_get_all_tags,
    obsidian_rename_tag,
    obsidian_bulk_move,
    obsidian_vault_analytics,
    obsidian_get_note_links,
]
