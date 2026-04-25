#!/bin/bash
# Reproduce the exact subprocess call gemini_b makes — argv array, no shell.
# This isolates the CLI's behaviour from any shell-quoting interference.
set -e
cd /app
python3 - <<'PY'
import asyncio, os, sys
async def main():
    env = os.environ.copy()
    if env.get("GEMINI_API_KEY_B"):
        env["GEMINI_API_KEY"] = env["GEMINI_API_KEY_B"]
    env.setdefault("TERM", "xterm-256color")
    env["NO_COLOR"] = "1"
    proc = await asyncio.create_subprocess_exec(
        "gemini", "--skip-trust", "--prompt", "Reply with the single word pong.",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    print("=== exit code ===", proc.returncode)
    print("=== stdout ===")
    print((stdout or b"").decode(errors="replace"))
    print("=== stderr ===")
    print((stderr or b"").decode(errors="replace"))
asyncio.run(main())
PY
