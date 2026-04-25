#!/bin/bash
# Verify Legion can reach super-agent's /memory/search and that the
# augmented query actually carries shared-memory context.
set -e
cd /app
python3 - <<'PY'
import asyncio, os
import sys
sys.path.insert(0, "/app")
from app.memory_client import fetch_relevant, augment_query

base = os.environ.get("SUPER_AGENT_BASE_URL", "<unset, will use code default>")
print(f"SUPER_AGENT_BASE_URL env = {base!r}")

async def main():
    results = await fetch_relevant("Bridge company workflow lead generation", limit=3)
    print(f"fetch_relevant returned {len(results)} result(s)")
    for r in results[:3]:
        text = (r.get("text") or r.get("content") or r.get("summary") or "")[:120]
        print(f"  - {text}")
    print()
    aug = await augment_query("hello world")
    print(f"augment_query length: {len(aug)} chars (original was 11)")
    if "[shared-memory context" in aug:
        print("CONTEXT BLOCK PRESENT — bridge is live")
    else:
        print("no context block — bridge inactive or no relevant memories")

asyncio.run(main())
PY
