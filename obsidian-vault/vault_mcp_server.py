#!/usr/bin/env python3
"""
Obsidian Vault MCP Server — lightweight Python MCP server over SSE/HTTP.

Serves on http://0.0.0.0:22360
MCP endpoint: http://obsidian-vault.railway.internal:22360/sse

Tools: list_directory, read_file, write_file, append_to_file,
       search_files, delete_file, get_vault_info
"""

import json
import logging
import os
import re
from pathlib import Path

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount

logging.basicConfig(level=logging.INFO, format="[vault-mcp] %(message)s")
logger = logging.getLogger(__name__)

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault"))
VAULT_PATH.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("MCP_PORT", 22360))

server = Server("obsidian-vault")


def _rel(path: str) -> Path:
    clean = Path(path.lstrip("/"))
    resolved = (VAULT_PATH / clean).resolve()
    if not str(resolved).startswith(str(VAULT_PATH.resolve())):
        raise ValueError(f"Path escapes vault: {path}")
    return resolved


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="list_directory",   description="List markdown notes in vault or subfolder. Leave path empty for all notes.", inputSchema={"type":"object","properties":{"path":{"type":"string","default":""}},"required":[]}),
        Tool(name="read_file",        description="Read full content of a note. Pass relative path e.g. 'Welcome.md'.",          inputSchema={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}),
        Tool(name="write_file",       description="Create or overwrite a note. path=relative .md path, content=markdown text.",  inputSchema={"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}),
        Tool(name="append_to_file",   description="Append text to a note without overwriting. Creates file if missing.",         inputSchema={"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}),
        Tool(name="search_files",     description="Full-text search across all notes. Case-insensitive.",                        inputSchema={"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}),
        Tool(name="delete_file",      description="Delete a note from the vault.",                                               inputSchema={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}),
        Tool(name="get_vault_info",   description="Vault stats: note count, size, folders. Use to verify vault is accessible.",  inputSchema={"type":"object","properties":{},"required":[]}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception as exc:
        result = f"[error] {exc}"
    return [TextContent(type="text", text=result)]


def _dispatch(name: str, args: dict) -> str:
    if name == "list_directory":
        path = args.get("path", "")
        base = _rel(path) if path else VAULT_PATH
        if not base.exists():
            return f"Directory not found: {path}"
        files = sorted(
            str(p.relative_to(VAULT_PATH))
            for p in base.rglob("*.md")
            if not any(part.startswith(".") for part in p.parts)
        )
        return "\n".join(files) if files else "(vault is empty)"

    elif name == "read_file":
        target = _rel(args["path"])
        return target.read_text(encoding="utf-8") if target.exists() else f"Note not found: {args['path']}"

    elif name == "write_file":
        target = _rel(args["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"], encoding="utf-8")
        return f"Written: {args['path']} ({len(args['content'])} chars)"

    elif name == "append_to_file":
        target = _rel(args["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        sep = "\n" if existing and not existing.endswith("\n") else ""
        target.write_text(existing + sep + args["content"], encoding="utf-8")
        return f"Appended {len(args['content'])} chars to: {args['path']}"

    elif name == "search_files":
        query = args["query"]
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results = []
        for md in sorted(VAULT_PATH.rglob("*.md")):
            if any(p.startswith(".") for p in md.parts):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            hits = [f"  line {i+1}: {l.strip()}" for i, l in enumerate(text.splitlines()) if pattern.search(l)]
            if hits:
                results.append(str(md.relative_to(VAULT_PATH)) + ":\n" + "\n".join(hits))
        return "\n\n".join(results) if results else f"No matches for: {query}"

    elif name == "delete_file":
        target = _rel(args["path"])
        if not target.exists():
            return f"Note not found: {args['path']}"
        target.unlink()
        return f"Deleted: {args['path']}"

    elif name == "get_vault_info":
        notes = [n for n in VAULT_PATH.rglob("*.md") if not any(p.startswith(".") for p in n.parts)]
        size = sum(n.stat().st_size for n in notes)
        folders = sorted({str(n.relative_to(VAULT_PATH).parent) for n in notes if n.parent != VAULT_PATH})
        return json.dumps({"vault_path": str(VAULT_PATH), "note_count": len(notes), "total_size_kb": round(size/1024,1), "folders": folders or ["(root only)"]}, indent=2)

    return f"Unknown tool: {name}"


def make_app():
    sse = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    return Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages", app=sse.handle_post_message),
    ])


if __name__ == "__main__":
    notes = list(VAULT_PATH.rglob("*.md"))
    logger.info("Vault MCP server starting on http://0.0.0.0:%d/sse", PORT)
    logger.info("Vault path: %s (%d notes found)", VAULT_PATH, len(notes))
    uvicorn.run(make_app(), host="0.0.0.0", port=PORT, log_level="warning")
