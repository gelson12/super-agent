#!/usr/bin/env python3
"""
Obsidian Vault MCP Server — lightweight Python MCP server over SSE/HTTP.

Serves on http://0.0.0.0:22360
MCP endpoint: http://obsidian-vault.railway.internal:22360/sse

Tools: list_directory, read_file, write_file, append_to_file,
       search_files, delete_file, get_vault_info,
       list_folders, get_recent_notes, get_vault_summary, move_file, search_by_tag,
       get_note_metadata, archive_old_notes,
       update_note_frontmatter, get_backlinks, search_with_filters,
       rename_note_with_backlink_update, create_note_from_template,
       get_all_tags, rename_tag_everywhere, bulk_move_files,
       get_vault_analytics, get_note_links
"""

import json
import logging
import os
import re
import time
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
        Tool(name="search_files",     description="Full-text search across all notes. Case-insensitive. max_results caps file matches (default 50, max 200).", inputSchema={"type":"object","properties":{"query":{"type":"string"},"max_results":{"type":"integer","default":50}},"required":["query"]}),
        Tool(name="delete_file",      description="Delete a note from the vault.",                                               inputSchema={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}),
        Tool(name="get_vault_info",   description="Vault stats: note count, size, folders. Use to verify vault is accessible.",  inputSchema={"type":"object","properties":{},"required":[]}),
        Tool(name="list_folders",     description="List all folder names in the vault (no files). Use to navigate the vault structure.", inputSchema={"type":"object","properties":{},"required":[]}),
        Tool(name="get_recent_notes", description="Return the N most recently modified notes with timestamps. Default N=10.",           inputSchema={"type":"object","properties":{"n":{"type":"integer","default":10}},"required":[]}),
        Tool(name="get_vault_summary",description="Return a digest of all notes grouped by folder with note names and word counts.",    inputSchema={"type":"object","properties":{},"required":[]}),
        Tool(name="move_file",        description="Move or rename a note. from_path and to_path are relative .md paths.",              inputSchema={"type":"object","properties":{"from_path":{"type":"string"},"to_path":{"type":"string"}},"required":["from_path","to_path"]}),
        Tool(name="search_by_tag",    description="Find notes that contain a YAML frontmatter tag or inline #tag. Case-insensitive.", inputSchema={"type":"object","properties":{"tag":{"type":"string"}},"required":["tag"]}),
        Tool(name="get_note_metadata",description="Read YAML frontmatter only (tags, date, type) without loading full note content. Fast metadata scan.", inputSchema={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}),
        Tool(name="archive_old_notes",description="Move notes older than N days into Archive/YYYY-MM/ folder. Default days=90. Keeps active vault clean.", inputSchema={"type":"object","properties":{"days":{"type":"integer","default":90},"dry_run":{"type":"boolean","default":False}},"required":[]}),
        Tool(name="update_note_frontmatter", description="Patch YAML frontmatter fields without touching the note body. Merges new fields into existing frontmatter. Creates frontmatter if note has none.", inputSchema={"type":"object","properties":{"path":{"type":"string"},"fields":{"type":"object"},"merge":{"type":"boolean","default":True}},"required":["path","fields"]}),
        Tool(name="get_backlinks", description="Find all notes that link to a given note via [[wikilink]] or markdown link syntax. Returns the referencing file paths.", inputSchema={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}),
        Tool(name="search_with_filters", description="Advanced search combining body text regex with frontmatter/tag/path filters. All filters are optional — combine as needed.", inputSchema={"type":"object","properties":{"content_query":{"type":"string","default":""},"tag":{"type":"string","default":""},"path_prefix":{"type":"string","default":""},"frontmatter_key":{"type":"string","default":""},"frontmatter_value":{"type":"string","default":""}},"required":[]}),
        Tool(name="rename_note_with_backlink_update", description="Rename or move a note AND update every [[wikilink]] reference to it across the entire vault. Safe rename that keeps the knowledge graph intact.", inputSchema={"type":"object","properties":{"from_path":{"type":"string"},"to_path":{"type":"string"}},"required":["from_path","to_path"]}),
        Tool(name="create_note_from_template", description="Create a new note from a template file. Substitutes {{variable}} placeholders with provided values. Template must exist in _templates/ folder.", inputSchema={"type":"object","properties":{"template_path":{"type":"string"},"new_note_path":{"type":"string"},"variables":{"type":"object","default":{}}},"required":["template_path","new_note_path"]}),
        Tool(name="get_all_tags", description="List every tag used across the vault with note counts. Includes YAML frontmatter tags and inline #hashtags. Returns JSON sorted by frequency.", inputSchema={"type":"object","properties":{},"required":[]}),
        Tool(name="rename_tag_everywhere", description="Rename a tag across every note in the vault — updates both YAML frontmatter lists and inline #hashtags.", inputSchema={"type":"object","properties":{"old_tag":{"type":"string"},"new_tag":{"type":"string"}},"required":["old_tag","new_tag"]}),
        Tool(name="bulk_move_files", description="Move all notes matching a glob pattern (e.g. 'Inbox/*.md') to a destination folder. Returns a count and list of moved files.", inputSchema={"type":"object","properties":{"pattern":{"type":"string"},"destination_folder":{"type":"string"}},"required":["pattern","destination_folder"]}),
        Tool(name="get_vault_analytics", description="Vault health report: orphaned notes (no links in or out), dead links (point to missing notes), link density, and per-folder stats.", inputSchema={"type":"object","properties":{},"required":[]}),
        Tool(name="get_note_links", description="Extract all outgoing links from a note: wikilinks [[Note]], markdown links [text](./note.md), and external URLs. Returns structured JSON.", inputSchema={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}),
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
        max_results = max(1, min(int(args.get("max_results", 50)), 200))
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results = []
        for md in sorted(VAULT_PATH.rglob("*.md")):
            if len(results) >= max_results:
                break
            if any(p.startswith(".") for p in md.parts):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            hits = [f"  line {i+1}: {l.strip()}" for i, l in enumerate(text.splitlines()) if pattern.search(l)]
            if hits:
                results.append(str(md.relative_to(VAULT_PATH)) + ":\n" + "\n".join(hits[:20]))
        suffix = f"\n\n(results capped at {max_results})" if len(results) >= max_results else ""
        return ("\n\n".join(results) + suffix) if results else f"No matches for: {query}"

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

    elif name == "list_folders":
        folders = sorted({
            str(p.relative_to(VAULT_PATH).parent)
            for p in VAULT_PATH.rglob("*.md")
            if not any(part.startswith(".") for part in p.parts)
            and p.parent != VAULT_PATH
        })
        root_files = list(VAULT_PATH.glob("*.md"))
        result = (["(root)"] if root_files else []) + folders
        return "\n".join(result) if result else "(no folders found)"

    elif name == "get_recent_notes":
        n = max(1, min(int(args.get("n", 10)), 100))
        notes = [
            p for p in VAULT_PATH.rglob("*.md")
            if not any(part.startswith(".") for part in p.parts)
        ]
        notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        lines = []
        for p in notes[:n]:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
            lines.append(f"{mtime}  {p.relative_to(VAULT_PATH)}")
        return "\n".join(lines) if lines else "(vault is empty)"

    elif name == "get_vault_summary":
        by_folder: dict = {}
        for p in sorted(VAULT_PATH.rglob("*.md")):
            if any(part.startswith(".") for part in p.parts):
                continue
            folder = str(p.relative_to(VAULT_PATH).parent)
            try:
                wc = len(p.read_text(encoding="utf-8").split())
            except Exception:
                wc = 0
            by_folder.setdefault(folder, []).append(f"  - {p.stem} ({wc}w)")
        if not by_folder:
            return "(vault is empty)"
        lines = []
        for folder in sorted(by_folder):
            label = folder if folder != "." else "(root)"
            lines.append(f"**{label}/** ({len(by_folder[folder])} notes)")
            lines.extend(by_folder[folder])
        return "\n".join(lines)

    elif name == "move_file":
        src = _rel(args["from_path"])
        dst = _rel(args["to_path"])
        if not src.exists():
            return f"Source not found: {args['from_path']}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return f"Moved: {args['from_path']} → {args['to_path']}"

    elif name == "search_by_tag":
        tag = args["tag"].lstrip("#").lower()
        # Match YAML frontmatter tags (tags: [foo, bar] or - foo) and inline #tag
        pat_inline   = re.compile(rf"#\b{re.escape(tag)}\b", re.IGNORECASE)
        pat_yaml_val = re.compile(rf"(?:^|\s|-\s+){re.escape(tag)}(?:\s|,|$)", re.IGNORECASE)
        results = []
        for md in sorted(VAULT_PATH.rglob("*.md")):
            if any(p.startswith(".") for p in md.parts):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if pat_inline.search(text) or pat_yaml_val.search(text):
                results.append(str(md.relative_to(VAULT_PATH)))
        return "\n".join(results) if results else f"No notes found with tag: #{tag}"

    elif name == "get_note_metadata":
        target = _rel(args["path"])
        if not target.exists():
            return f"Note not found: {args['path']}"
        text = target.read_text(encoding="utf-8")
        fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
        if not fm_match:
            return json.dumps({"path": args["path"], "has_frontmatter": False, "raw": ""})
        raw_fm = fm_match.group(1)
        meta: dict = {"path": args["path"], "has_frontmatter": True}
        for line in raw_fm.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        return json.dumps(meta, indent=2)

    elif name == "archive_old_notes":
        days    = int(args.get("days", 90))
        dry_run = bool(args.get("dry_run", False))
        cutoff  = time.time() - days * 86400
        moved, skipped = [], []
        for md in list(VAULT_PATH.rglob("*.md")):
            if any(p.startswith(".") for p in md.parts):
                continue
            rel = md.relative_to(VAULT_PATH)
            if str(rel).startswith("Archive"):
                continue
            if md.stat().st_mtime < cutoff:
                mtime = time.localtime(md.stat().st_mtime)
                dest_dir = VAULT_PATH / "Archive" / time.strftime("%Y-%m", mtime)
                dest = dest_dir / md.name
                if not dry_run:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    md.rename(dest)
                moved.append(str(rel))
            else:
                skipped.append(str(rel))
        prefix = "[DRY RUN] Would move" if dry_run else "Moved"
        return json.dumps({"archived": len(moved), "kept": len(skipped),
                           "note": f"{prefix} {len(moved)} notes older than {days} days to Archive/",
                           "files": moved}, indent=2)

    elif name == "update_note_frontmatter":
        target = _rel(args["path"])
        new_fields: dict = args.get("fields", {})
        merge = bool(args.get("merge", True))
        if not target.exists():
            # Create note with just frontmatter
            fm_lines = ["---"]
            for k, v in new_fields.items():
                fm_lines.append(f"{k}: {json.dumps(v) if isinstance(v, (list, dict)) else v}")
            fm_lines.append("---\n")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(fm_lines), encoding="utf-8")
            return f"Created {args['path']} with frontmatter: {list(new_fields.keys())}"
        text = target.read_text(encoding="utf-8")
        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)', text, re.DOTALL)
        if fm_match:
            raw_fm, body = fm_match.group(1), fm_match.group(2)
            # Parse existing fields
            existing: dict = {}
            for line in raw_fm.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    existing[k.strip()] = v.strip()
            if merge:
                existing.update(new_fields)
            else:
                existing = new_fields
        else:
            existing = new_fields
            body = text
        fm_lines = ["---"]
        for k, v in existing.items():
            fm_lines.append(f"{k}: {json.dumps(v) if isinstance(v, (list, dict)) else v}")
        fm_lines.append("---")
        new_text = "\n".join(fm_lines) + "\n" + body.lstrip("\n")
        target.write_text(new_text, encoding="utf-8")
        return json.dumps({"path": args["path"], "updated_fields": list(new_fields.keys()), "merged": merge})

    elif name == "get_backlinks":
        target_path = args["path"]
        # Build set of names to match: stem and full relative path
        target_stem = Path(target_path).stem
        results = []
        pat_wiki = re.compile(rf'\[\[{re.escape(target_stem)}(?:\|[^\]]+)?\]\]', re.IGNORECASE)
        pat_md   = re.compile(rf'\[.*?\]\(\.?/?\s*{re.escape(target_path)}\s*\)', re.IGNORECASE)
        for md in sorted(VAULT_PATH.rglob("*.md")):
            if any(p.startswith(".") for p in md.parts):
                continue
            rel = str(md.relative_to(VAULT_PATH))
            if rel == target_path:
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if pat_wiki.search(text) or pat_md.search(text):
                results.append(rel)
        return json.dumps({"path": target_path, "backlinks": results, "count": len(results)}, indent=2)

    elif name == "search_with_filters":
        content_query  = args.get("content_query", "").strip()
        tag_filter     = args.get("tag", "").strip().lstrip("#").lower()
        path_prefix    = args.get("path_prefix", "").strip()
        fm_key         = args.get("frontmatter_key", "").strip()
        fm_val         = args.get("frontmatter_value", "").strip().lower()
        content_pat    = re.compile(content_query, re.IGNORECASE) if content_query else None
        results = []
        for md in sorted(VAULT_PATH.rglob("*.md")):
            if any(p.startswith(".") for p in md.parts):
                continue
            rel = str(md.relative_to(VAULT_PATH))
            if path_prefix and not rel.lower().startswith(path_prefix.lower()):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            # Frontmatter parse
            fm_data: dict = {}
            fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if fm_match:
                for line in fm_match.group(1).splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        fm_data[k.strip().lower()] = v.strip().lower()
            if tag_filter:
                tag_in_fm = any(tag_filter in v for v in fm_data.values())
                tag_inline = bool(re.search(rf'#\b{re.escape(tag_filter)}\b', text, re.IGNORECASE))
                if not tag_in_fm and not tag_inline:
                    continue
            if fm_key and fm_val:
                if fm_data.get(fm_key.lower(), "") != fm_val:
                    continue
            elif fm_key:
                if fm_key.lower() not in fm_data:
                    continue
            if content_pat and not content_pat.search(text):
                continue
            hits = []
            if content_pat:
                hits = [f"  line {i+1}: {l.strip()}" for i, l in enumerate(text.splitlines()) if content_pat.search(l)]
            results.append(rel + (":\n" + "\n".join(hits) if hits else ""))
        return "\n\n".join(results) if results else "No notes match the given filters."

    elif name == "rename_note_with_backlink_update":
        src = _rel(args["from_path"])
        dst = _rel(args["to_path"])
        if not src.exists():
            return f"Source not found: {args['from_path']}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        old_stem = src.stem
        new_stem = dst.stem
        # Move the file first
        src.rename(dst)
        # Update all backlinks across vault
        updated_files = []
        pat = re.compile(rf'\[\[{re.escape(old_stem)}((?:\|[^\]]+)?)\]\]', re.IGNORECASE)
        for md in VAULT_PATH.rglob("*.md"):
            if any(p.startswith(".") for p in md.parts):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            new_text = pat.sub(rf'[[{new_stem}\1]]', text)
            if new_text != text:
                md.write_text(new_text, encoding="utf-8")
                updated_files.append(str(md.relative_to(VAULT_PATH)))
        return json.dumps({
            "moved": f"{args['from_path']} → {args['to_path']}",
            "backlinks_updated": len(updated_files),
            "files_updated": updated_files,
        }, indent=2)

    elif name == "create_note_from_template":
        tpl_path   = _rel(args["template_path"])
        note_path  = _rel(args["new_note_path"])
        variables  = args.get("variables", {})
        if not tpl_path.exists():
            return f"Template not found: {args['template_path']}"
        if note_path.exists():
            return f"Note already exists: {args['new_note_path']} — use write_file to overwrite"
        content = tpl_path.read_text(encoding="utf-8")
        # Standard built-in variables
        import datetime as _dt
        variables.setdefault("date",  _dt.date.today().isoformat())
        variables.setdefault("time",  _dt.datetime.utcnow().strftime("%H:%M UTC"))
        variables.setdefault("title", Path(args["new_note_path"]).stem)
        for k, v in variables.items():
            content = content.replace("{{" + k + "}}", str(v))
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(content, encoding="utf-8")
        return f"Created {args['new_note_path']} from template {args['template_path']} ({len(content)} chars)"

    elif name == "get_all_tags":
        tag_counts: dict = {}
        pat_inline = re.compile(r'#([A-Za-z0-9_/\-]+)')
        pat_yaml_tag = re.compile(r'^\s*-?\s*([A-Za-z0-9_/\-]+)\s*$')
        for md in VAULT_PATH.rglob("*.md"):
            if any(p.startswith(".") for p in md.parts):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            # YAML frontmatter tags
            fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if fm_match:
                in_tags = False
                for line in fm_match.group(1).splitlines():
                    if re.match(r'^tags\s*:', line, re.IGNORECASE):
                        in_tags = True
                        # inline list: tags: [a, b, c]
                        inner = re.search(r'\[(.+)\]', line)
                        if inner:
                            for t in inner.group(1).split(","):
                                t = t.strip().strip('"\'')
                                if t:
                                    tag_counts[t.lower()] = tag_counts.get(t.lower(), 0) + 1
                        continue
                    if in_tags:
                        m = pat_yaml_tag.match(line)
                        if m:
                            tag_counts[m.group(1).lower()] = tag_counts.get(m.group(1).lower(), 0) + 1
                        elif line.strip() and not line.startswith(" ") and not line.startswith("-"):
                            in_tags = False
            # Inline #hashtags (skip frontmatter block)
            body = text[fm_match.end():] if fm_match else text
            for m in pat_inline.finditer(body):
                t = m.group(1).lower()
                tag_counts[t] = tag_counts.get(t, 0) + 1
        sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
        return json.dumps({"total_unique_tags": len(sorted_tags), "tags": dict(sorted_tags)}, indent=2)

    elif name == "rename_tag_everywhere":
        old_tag = args["old_tag"].lstrip("#").strip()
        new_tag = args["new_tag"].lstrip("#").strip()
        updated_files = []
        # Patterns: inline #old_tag and yaml list item
        pat_inline = re.compile(rf'#\b{re.escape(old_tag)}\b', re.IGNORECASE)
        pat_yaml   = re.compile(rf'(\s*-\s*){re.escape(old_tag)}(\s*$)', re.IGNORECASE | re.MULTILINE)
        for md in VAULT_PATH.rglob("*.md"):
            if any(p.startswith(".") for p in md.parts):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            new_text = pat_inline.sub(f"#{new_tag}", text)
            new_text = pat_yaml.sub(rf'\g<1>{new_tag}\g<2>', new_text)
            if new_text != text:
                md.write_text(new_text, encoding="utf-8")
                updated_files.append(str(md.relative_to(VAULT_PATH)))
        return json.dumps({"old_tag": old_tag, "new_tag": new_tag,
                           "notes_updated": len(updated_files), "files": updated_files}, indent=2)

    elif name == "bulk_move_files":
        pattern = args["pattern"]
        dest_folder = args["destination_folder"].strip("/")
        dest_dir = _rel(dest_folder)
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved = []
        skipped = []
        for md in list(VAULT_PATH.glob(pattern)):
            if any(p.startswith(".") for p in md.parts):
                continue
            if not md.suffix == ".md":
                continue
            dest = dest_dir / md.name
            if dest.exists():
                skipped.append(str(md.relative_to(VAULT_PATH)))
                continue
            md.rename(dest)
            moved.append(f"{md.relative_to(VAULT_PATH)} → {dest.relative_to(VAULT_PATH)}")
        return json.dumps({"moved": len(moved), "skipped": len(skipped),
                           "destination": dest_folder, "files": moved}, indent=2)

    elif name == "get_vault_analytics":
        all_notes = [
            md for md in VAULT_PATH.rglob("*.md")
            if not any(p.startswith(".") for p in md.parts)
        ]
        note_rels = {str(md.relative_to(VAULT_PATH)): md for md in all_notes}
        note_stems = {md.stem.lower() for md in all_notes}
        # Build outgoing link map and dead link list
        outgoing: dict[str, list] = {}
        dead_links: list = []
        pat_wiki = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')
        for rel, md in note_rels.items():
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            links = [m.group(1).strip() for m in pat_wiki.finditer(text)]
            outgoing[rel] = links
            for lk in links:
                if lk.lower() not in note_stems and lk + ".md" not in note_rels:
                    dead_links.append({"from": rel, "broken_link": lk})
        # Backlink map
        has_backlink = set()
        for rel, links in outgoing.items():
            for lk in links:
                for nrel in note_rels:
                    if Path(nrel).stem.lower() == lk.lower():
                        has_backlink.add(nrel)
        # Orphaned: no outgoing links AND no incoming links
        orphaned = [
            rel for rel in note_rels
            if not outgoing.get(rel) and rel not in has_backlink
        ]
        # Per-folder stats
        folder_counts: dict = {}
        for rel in note_rels:
            folder = str(Path(rel).parent)
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
        avg_links = round(
            sum(len(v) for v in outgoing.values()) / max(len(outgoing), 1), 2
        )
        return json.dumps({
            "total_notes": len(all_notes),
            "orphaned_notes": orphaned,
            "dead_links": dead_links[:50],
            "avg_outgoing_links_per_note": avg_links,
            "notes_per_folder": folder_counts,
        }, indent=2)

    elif name == "get_note_links":
        target = _rel(args["path"])
        if not target.exists():
            return f"Note not found: {args['path']}"
        text = target.read_text(encoding="utf-8")
        wikilinks = [m.group(1).strip() for m in re.finditer(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', text)]
        md_links   = re.findall(r'\[.*?\]\(([^)]+)\)', text)
        ext_links  = [lk for lk in md_links if lk.startswith("http")]
        int_links  = [lk for lk in md_links if not lk.startswith("http")]
        return json.dumps({
            "path": args["path"],
            "wikilinks": wikilinks,
            "internal_markdown_links": int_links,
            "external_urls": ext_links,
            "total_outgoing": len(wikilinks) + len(md_links),
        }, indent=2)

    logger.warning("vault_mcp: unknown tool requested: %s (args: %s)", name, list(args.keys()))
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
