"""
Unified Memory Sync — bridges Claude Code local memory ↔ Railway shared DB.

Two operations:
  push  — read all .md files from Claude Code's memory directory and POST them
           to /memory/ingest on the CLI worker (inspiring-cat). Memories are
           stored in PostgreSQL with source="claude_code" and made available
           to all API models on the next request.

  pull  — fetch recent important memories from /memory/export and write them
           as a single markdown file into Claude Code's memory directory so
           Claude Code has visibility into what API sessions learned.

  both  — push then pull (default)

Usage:
  python scripts/sync_memory.py [push|pull|both] [--dry-run]

Environment (set in .env or as shell exports):
  CLI_WORKER_URL   — base URL of CLI worker service, e.g.
                     https://inspiring-cat-production.up.railway.app
  MEMORY_SECRET    — value of MEMORY_INGEST_SECRET Railway env var (optional)
  CLAUDE_MEMORY_DIR — path to Claude Code project memory directory
                      defaults to C:/Users/Gelson/.claude/projects/
                        c--Users-Gelson-Downloads-bjj-video-analysis/memory
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

# Load .env if present
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

CLI_WORKER_URL = os.environ.get(
    "CLI_WORKER_URL",
    "https://inspiring-cat-production.up.railway.app",
).rstrip("/")

MEMORY_SECRET = os.environ.get("MEMORY_INGEST_SECRET", "")

CLAUDE_MEMORY_DIR = Path(os.environ.get(
    "CLAUDE_MEMORY_DIR",
    r"C:\Users\Gelson\.claude\projects\c--Users-Gelson-Downloads-bjj-video-analysis\memory",
))

DRY_RUN = "--dry-run" in sys.argv


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post(path: str, body: dict) -> dict:
    url = f"{CLI_WORKER_URL}{path}"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if MEMORY_SECRET:
        headers["X-Memory-Secret"] = MEMORY_SECRET
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get(path: str) -> dict:
    url = f"{CLI_WORKER_URL}{path}"
    headers = {}
    if MEMORY_SECRET:
        headers["X-Memory-Secret"] = MEMORY_SECRET
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _parse_memory_file(path: Path) -> list[dict]:
    """
    Parse a Claude Code memory markdown file into memory items.
    Reads the frontmatter type/name and the body as the content.
    Returns a list of memory dicts ready for /memory/ingest.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    mem_type = "fact"
    importance = 3
    name = path.stem

    # Parse YAML-ish frontmatter between --- markers
    in_front = False
    body_lines = []
    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            if line.strip() == "---":
                in_front = False
                continue
            if line.startswith("type:"):
                t = line.split(":", 1)[1].strip()
                type_map = {
                    "user": "preference",
                    "feedback": "preference",
                    "project": "fact",
                    "reference": "fact",
                }
                mem_type = type_map.get(t, "fact")
                if t in ("feedback",):
                    importance = 4
                elif t in ("user",):
                    importance = 3
            elif line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if not body or len(body) < 30:
        return []

    # Split long files into chunks of ~800 chars to stay within DB limits
    chunks = []
    current = []
    current_len = 0
    for para in body.split("\n\n"):
        if current_len + len(para) > 800 and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para)
    if current:
        chunks.append("\n\n".join(current))

    items = []
    for i, chunk in enumerate(chunks):
        label = name if i == 0 else f"{name} (part {i+1})"
        items.append({
            "content": f"[Memory: {label}]\n{chunk[:800]}",
            "memory_type": mem_type,
            "importance": importance,
            "source": "claude_code",
            "session_id": "claude_code_sync",
        })
    return items


# ── Push ──────────────────────────────────────────────────────────────────────

def push():
    """Push all Claude Code memory .md files to the shared DB."""
    if not CLAUDE_MEMORY_DIR.exists():
        print(f"[push] Memory dir not found: {CLAUDE_MEMORY_DIR}")
        return

    md_files = [f for f in CLAUDE_MEMORY_DIR.glob("*.md")
                if f.name.upper() != "MEMORY.MD"]

    if not md_files:
        print("[push] No memory files found.")
        return

    all_items = []
    for f in md_files:
        try:
            items = _parse_memory_file(f)
            all_items.extend(items)
            if items:
                print(f"[push] Parsed {f.name} → {len(items)} chunk(s)")
        except Exception as e:
            print(f"[push] Skipping {f.name}: {e}")

    if not all_items:
        print("[push] Nothing to push.")
        return

    print(f"[push] Pushing {len(all_items)} memory chunks to {CLI_WORKER_URL}/memory/ingest ...")
    if DRY_RUN:
        print("[push] DRY RUN — skipping actual POST")
        for item in all_items[:3]:
            print(f"  {item['content'][:80]}...")
        return

    # Batch into groups of 50 to avoid large payloads
    batch_size = 50
    total_saved = 0
    for i in range(0, len(all_items), batch_size):
        batch = all_items[i:i + batch_size]
        try:
            result = _post("/memory/ingest", {"memories": batch})
            total_saved += result.get("saved", 0)
        except Exception as e:
            print(f"[push] Batch {i//batch_size + 1} failed: {e}")

    print(f"[push] Done — {total_saved}/{len(all_items)} chunks stored in shared DB ✓")


# ── Pull ──────────────────────────────────────────────────────────────────────

def pull():
    """Fetch recent important memories from DB and write to local memory file."""
    print(f"[pull] Fetching memories from {CLI_WORKER_URL}/memory/export ...")
    try:
        result = _get("/memory/export?limit=200&min_importance=3")
    except Exception as e:
        print(f"[pull] Failed: {e}")
        return

    memories = result.get("memories", [])
    if not memories:
        print("[pull] No memories returned.")
        return

    # Filter out memories we already wrote (source=claude_code)
    external = [m for m in memories if m.get("source", "") != "claude_code"]
    if not external:
        print("[pull] All memories are already from claude_code source — nothing new.")
        return

    # Group by source for the markdown output
    by_source: dict[str, list] = {}
    for m in external:
        src = m.get("source", "unknown")
        by_source.setdefault(src, []).append(m)

    # Write to memory dir
    if not CLAUDE_MEMORY_DIR.exists():
        print(f"[pull] Memory dir not found: {CLAUDE_MEMORY_DIR}")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = CLAUDE_MEMORY_DIR / f"shared_insights_{now}.md"

    lines = [
        "---",
        "name: Shared cross-agent insights",
        f"description: Memories accumulated from all agents as of {now}",
        "type: project",
        "---",
        "",
        f"## Cross-Agent Shared Memory (synced {now})",
        "",
    ]

    for src, mems in by_source.items():
        lines.append(f"### Source: {src}")
        lines.append("")
        for m in mems[:20]:  # cap per source to keep file manageable
            content = m["content"]
            # Strip internal tags for readability
            import re
            content = re.sub(r"\[IMPORTANT:[^\]]+\]", "", content).strip()
            content = re.sub(r"\[source:[^\]]+\]", "", content).strip()
            if content:
                lines.append(f"- {content[:300]}")
        lines.append("")

    md_text = "\n".join(lines)

    if DRY_RUN:
        print(f"[pull] DRY RUN — would write {len(external)} memories to {out_path}")
        print(md_text[:500])
        return

    out_path.write_text(md_text, encoding="utf-8")
    print(f"[pull] Written {len(external)} memories to {out_path} ✓")

    # Also update MEMORY.md index
    _update_memory_index(out_path, len(external))


def _update_memory_index(new_file: Path, count: int):
    """Add the synced insights file to MEMORY.md index."""
    try:
        index_path = CLAUDE_MEMORY_DIR / "MEMORY.md"
        if not index_path.exists():
            return
        content = index_path.read_text(encoding="utf-8")
        entry_name = new_file.name
        hook = f"{count} cross-agent insights from DB"
        new_entry = f"- [{entry_name}]({entry_name}) — {hook}"
        # Replace existing entry for same date or append
        import re
        pattern = re.compile(r"- \[shared_insights_[^\]]+\]\([^)]+\)[^\n]*")
        if pattern.search(content):
            content = pattern.sub(new_entry, content, count=1)
        else:
            content = content.rstrip() + "\n" + new_entry + "\n"
        index_path.write_text(content, encoding="utf-8")
        print(f"[pull] MEMORY.md index updated ✓")
    except Exception as e:
        print(f"[pull] MEMORY.md update skipped: {e}")


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats():
    """Show memory stats from the shared DB."""
    try:
        result = _get("/memory/stats")
        print(f"\nShared Memory Stats — {CLI_WORKER_URL}")
        print(f"  Total memories: {result['total_memories']}")
        print(f"  Last 24h:       {result.get('last_24h', 'N/A')}")
        print(f"\n  By source:")
        for s in result.get("by_source", []):
            print(f"    {s['source']:25s} {s['count']:5d}  last: {s.get('last_write','?')[:19]}")
    except Exception as e:
        print(f"Stats failed: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys
    # Force line-buffered output so PowerShell shows progress immediately
    _sys.stdout.reconfigure(line_buffering=True)

    op = "both"
    for arg in sys.argv[1:]:
        if arg in ("push", "pull", "both", "stats"):
            op = arg

    print(f"[sync_memory] Starting — op={op}", flush=True)
    print(f"[sync_memory] CLI_WORKER_URL = {CLI_WORKER_URL}", flush=True)
    print(f"[sync_memory] MEMORY_SECRET  = {'<set>' if MEMORY_SECRET else '<NOT SET — will be rejected>'}", flush=True)
    print(f"[sync_memory] MEMORY_DIR     = {CLAUDE_MEMORY_DIR}", flush=True)
    print(flush=True)

    if op == "push":
        push()
    elif op == "pull":
        pull()
    elif op == "stats":
        stats()
    else:
        push()
        print(flush=True)
        pull()
        print(flush=True)
        stats()
