#!/usr/bin/env python3
"""
Obsidian Vault MCP Server — lightweight Python replacement for the Obsidian
Claude Code MCP plugin.

Exposes the vault at /vault as MCP tools over WebSocket on port 22360.
Files are plain markdown — fully compatible with Obsidian when opened locally.

Tools exposed (matching Ian Sinnott's plugin naming conventions):
  - list_directory    list notes in vault or a subfolder
  - read_file         read a note's content
  - write_file        create or overwrite a note
  - append_to_file    append to an existing note
  - search_files      full-text search across all notes
  - delete_file       delete a note
  - get_vault_info    vault stats (note count, size)
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="[vault-mcp] %(message)s")
logger = logging.getLogger(__name__)

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault"))
VAULT_PATH.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("obsidian-vault")


def _rel(path: str) -> Path:
    """Resolve a relative vault path safely — no directory traversal."""
    clean = Path(path.lstrip("/"))
    resolved = (VAULT_PATH / clean).resolve()
    if not str(resolved).startswith(str(VAULT_PATH.resolve())):
        raise ValueError(f"Path escapes vault: {path}")
    return resolved


@mcp.tool()
def list_directory(path: str = "") -> str:
    """
    List notes (markdown files) in the vault or a subdirectory.
    Returns a newline-separated list of relative file paths.
    Leave path empty to list all notes in the entire vault.
    """
    base = _rel(path) if path else VAULT_PATH
    if not base.exists():
        return f"Directory not found: {path}"
    files = sorted(
        str(p.relative_to(VAULT_PATH))
        for p in base.rglob("*.md")
        if not any(part.startswith(".") for part in p.parts)
    )
    if not files:
        return "(vault is empty)"
    return "\n".join(files)


@mcp.tool()
def read_file(path: str) -> str:
    """
    Read the full content of a note from the vault.
    Pass the relative file path (e.g. 'Welcome.md' or 'folder/note.md').
    """
    target = _rel(path)
    if not target.exists():
        return f"Note not found: {path}"
    return target.read_text(encoding="utf-8")


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """
    Create or overwrite a note in the vault.
    path: relative file path including .md extension (e.g. 'ideas/2026-04-15.md')
    content: full markdown content to write.
    """
    target = _rel(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Written: {path} ({len(content)} chars)"


@mcp.tool()
def append_to_file(path: str, content: str) -> str:
    """
    Append text to an existing note without overwriting it.
    Creates the file if it does not exist.
    path: relative file path (e.g. 'improvements/log.md')
    content: markdown text to append at the end of the file.
    """
    target = _rel(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    separator = "\n" if existing and not existing.endswith("\n") else ""
    target.write_text(existing + separator + content, encoding="utf-8")
    return f"Appended {len(content)} chars to: {path}"


@mcp.tool()
def search_files(query: str) -> str:
    """
    Full-text search across all notes in the vault.
    Returns matching file paths and the lines that contain the query.
    Case-insensitive.
    """
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    for md_file in sorted(VAULT_PATH.rglob("*.md")):
        if any(part.startswith(".") for part in md_file.parts):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        matches = [
            f"  line {i+1}: {line.strip()}"
            for i, line in enumerate(text.splitlines())
            if pattern.search(line)
        ]
        if matches:
            rel = str(md_file.relative_to(VAULT_PATH))
            results.append(f"{rel}:\n" + "\n".join(matches))
    if not results:
        return f"No matches found for: {query}"
    return "\n\n".join(results)


@mcp.tool()
def delete_file(path: str) -> str:
    """
    Delete a note from the vault.
    path: relative file path (e.g. 'old-note.md')
    """
    target = _rel(path)
    if not target.exists():
        return f"Note not found: {path}"
    target.unlink()
    return f"Deleted: {path}"


@mcp.tool()
def get_vault_info() -> str:
    """
    Get vault statistics: note count, total size, and list of top-level folders.
    Use this to verify the vault is accessible and check its contents at a glance.
    """
    notes = list(VAULT_PATH.rglob("*.md"))
    notes = [n for n in notes if not any(p.startswith(".") for p in n.parts)]
    total_size = sum(n.stat().st_size for n in notes)
    folders = sorted({
        str(n.relative_to(VAULT_PATH).parent)
        for n in notes
        if n.parent != VAULT_PATH
    })
    return json.dumps({
        "vault_path": str(VAULT_PATH),
        "note_count": len(notes),
        "total_size_kb": round(total_size / 1024, 1),
        "folders": folders or ["(root only)"],
    }, indent=2)


if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", 22360))
    logger.info("Vault MCP server starting on ws://0.0.0.0:%d", port)
    logger.info("Vault path: %s (%d notes found)",
                VAULT_PATH,
                len(list(VAULT_PATH.rglob("*.md"))))
    mcp.run(transport="websocket", host="0.0.0.0", port=port)
