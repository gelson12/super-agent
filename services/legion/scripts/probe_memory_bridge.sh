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
    queries = [
        "hello",
        "claude",
        "bridge digital",
        "Bridge company workflow lead generation",
    ]
    for q in queries:
        results = await fetch_relevant(q, limit=3)
        print(f"  q={q!r:50s} → {len(results)} result(s)")
    print()
    aug = await augment_query("what is bridge digital")
    print(f"augment_query length: {len(aug)} chars (original was 22)")
    if "[shared-memory context" in aug:
        print("CONTEXT BLOCK PRESENT — bridge is live")
        first_line = aug.split("\n", 5)
        for ln in first_line[:5]:
            print("  >", ln[:120])
    else:
        print("no context block — bridge inactive or no relevant memories")

asyncio.run(main())
PY
