"""
One-time seed script — pushes Claude Code local memories into the shared DB.
Writes directly via psycopg2 so it works even when Gemini embeddings are unavailable.

Run from inside the inspiring-cat container:
  cd /app && python scripts/seed_memories.py
  OR:
  curl -sL https://raw.githubusercontent.com/gelson12/super-agent/master/scripts/seed_memories.py -o /tmp/seed.py && python /tmp/seed.py
"""
import hashlib, json, os, sys

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", flush=True)
    sys.exit(1)

conn_str = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MEMORIES = [
  ("claude_code", "fact",       4, "Railway infrastructure: Project ID 4c972469-6e49-4ac5-9460-5e65b7eaba07, Env ID ab62ec1d-da97-44f4-83eb-489242f8ebf7. Services: VS-Code-inspiring-cat (7cf85f11, inspiring-cat-production.up.railway.app), super-agent (8e928dea, super-agent-production.up.railway.app), N8N (b47ae540, outstanding-blessing-production-1d4b.up.railway.app), Postgres (0293b0b8 internal), obsidian-vault (03f72dbd port 22360 internal), radiant-appreciation (18f8b3e3 bridge-digital-solution.com). RAILWAY_TOKEN: dbd31e84-6046-44d8-afd4-ee804a03a39d."),
  ("claude_code", "preference",  5, "CRITICAL: Claude CLI Pro, Gemini CLI, Playwright, PTY, and all recovery endpoints live ONLY in VS-Code-inspiring-cat (cli_worker/main.py port 8003). Super-agent uses Anthropic API only. NEVER put CLI/PTY/recovery code in super-agent — causes queue mismatch where magic links are silently lost because browser waits on inspiring-cat queue while full_recovery_chain() runs in super-agent process."),
  ("claude_code", "fact",       4, "CLI auth flow layers: L1 volume backup /workspace/.claude_credentials_backup.json (WORKING, instant on restart). L2 Railway API push (PERMANENTLY BLOCKED — Cloudflare HTTP 403 code 1010 blocks all Railway container IPs). L3 OAuth direct refresh claude.com/cai/oauth/token (PERMANENTLY BLOCKED — HTTP 405 from Railway NL IPs). L4 Playwright+n8n magic link (WORKING, ~5-15min). L5 GitHub Actions relay (IMPLEMENTED — dispatches to gelson12/super-agent, Azure IPs not CF-blocked). OAUTH_RELAY_SECRET set in both Railway and GitHub secrets."),
  ("claude_code", "fact",       4, "Unified memory system deployed 2026-04-16. Shared PostgreSQL table agent_memories (id, session_id, content, embedding vector(768), source, memory_type, importance, content_hash, created_at). Write sources: super_agent (every API exchange), auto_extract (Haiku distils complexity>=2 exchanges), cli_pro (after Claude CLI Pro tasks), gemini_cli (after Gemini CLI tasks), claude_code (sync_memory.py). MEMORY_INGEST_SECRET: 2dc6c69574c14d615ce146e54640aa13030dade7aa86697c. Endpoints: /memory/ingest POST, /memory/export GET, /memory/stats GET."),
  ("claude_code", "fact",       3, "Routing architecture (CLI-first since 2026-04-13): tier order = Claude CLI Pro (zero cost) > Gemini CLI (free 1500/day) > Haiku API (costs tokens). Keyword routing fires before classifier: GITHUB_KEYWORDS (website, html, instagram, bridge-digital-solution), SHELL_KEYWORDS (terminal, flutter, build, clone), N8N_KEYWORDS (workflow, n8n, automation, webhook). Confidence-arbitrated: AI conf>=0.75 overrides keywords; conf<0.4 escalates to peer-review. Drift-aware: win_rate<60% swaps to best alternative model."),
  ("claude_code", "fact",       3, "Super-agent repo at c:/Users/Gelson/Downloads/super-agent/. Key files: app/tools/n8n_tools.py (n8n REST API wrappers), app/agents/n8n_agent.py, app/mcp/n8n_mcp_server.py (MCP tools for Claude CLI), .mcp.json (MCP registration), n8n/business_hub_workflow.json (15-service webhook router). n8n URL: outstanding-blessing-production-1d4b.up.railway.app. Active workflows: jxnZZwTqJ7naPKc6 Claude-Verification-Monitor, ke7YzsAmGerVWVVc Super-Agent-Health-Monitor."),
  ("claude_code", "fact",       3, "Website bridge-digital-solution.com in repo under website/index.html. Railway service radiant-appreciation, auto-deploys on push. n8n three paths: 1) Python n8n tools, 2) run_shell_via_cli_worker() curl via inspiring-cat, 3) run_authorized_shell_command() via super-agent shell."),
  ("claude_code", "fact",       3, "Obsidian vault MCP: fully working 2026-04-15. Python FastAPI/SSE server at obsidian-vault.railway.internal:22360/sse. File: obsidian-vault/vault_mcp_server.py. Vault on volume vol_8zt6u1gqp3o57zvm at /vault. Registered in inspiring-cat via ~/.claude.json (Python merge in entrypoint.cli.sh). Tools: list_directory, read_file, write_file, append_to_file, search_files, delete_file, get_vault_info, create_note, move_file, list_tags, get_stats, read_note_links, search_by_tag, get_recent."),
  ("claude_code", "fact",       3, "CLI healing key files (all in inspiring-cat at /app): app/learning/cli_auto_login.py (PTY, browser, full_recovery_chain, gemini_full_recovery), cli_worker/main.py (FastAPI port 8003: /webhook/verification-code, /webhook/manual-auth-code, /webhook/github-oauth-result, /auth/login-status), cli_worker/task_runner.py (_dispatch detects auth errors triggers full_recovery_chain in background)."),
  ("claude_code", "fact",       3, "Pending bugs: 1) routing_fallback.py lines 81-85 calls claude-sonnet-4-6 directly via anthropic client bypassing ask_internal() — fix: replace with ask_internal(). 2) GEMINI.md may not auto-load in gemini CLI — fix: prepend GEMINI.md content to prompt in task_runner.py. High value: auto-update CLAUDE.md from weekly self-improve review."),
  ("claude_code", "fact",       3, "Dashboard: seed_live_status() fixed to clear sick->idle on recovery (was one-directional). 30-min health check calls it periodically. ANTHROPIC_API_KEY has no credits — user must add at console.anthropic.com. APK build: DeepSeek fallback working but server went down mid-build (resource issue), needs retry."),
  ("claude_code", "fact",       2, "Domain bridge-digital-solution.com MX records: add directly from Railway workspace domains page. Select domain, add DNS type MX for Zoho Mail. No nameserver change needed."),
  ("claude_code", "fact",       3, "GitHub Actions OAuth relay: workflow at .github/workflows/oauth_refresh.yml in gelson12/super-agent. Triggered by repository_dispatch type claude_oauth_refresh. Runner POSTs to claude.com/cai/oauth/token (Azure IPs not CF-blocked), callbacks to /webhook/github-oauth-result with OAUTH_RELAY_SECRET. Secret set in both Railway inspiring-cat vars and GitHub Actions secrets."),
]

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed", flush=True)
    sys.exit(1)

print(f"Connecting to PostgreSQL ...", flush=True)
try:
    conn = psycopg2.connect(conn_str)
    conn.autocommit = False
    cur = conn.cursor()
except Exception as e:
    print(f"ERROR: DB connect failed: {e}", flush=True)
    sys.exit(1)

# Ensure columns exist (idempotent)
for col, coltype in [("source","TEXT"), ("memory_type","TEXT"), ("importance","INT DEFAULT 3"), ("content_hash","TEXT")]:
    try:
        cur.execute(f"ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS {col} {coltype}")
    except Exception:
        conn.rollback()

try:
    cur.execute("ALTER TABLE agent_memories ADD CONSTRAINT agent_memories_content_hash_key UNIQUE (content_hash)")
    conn.commit()
except Exception:
    conn.rollback()

saved = 0
skipped = 0
for source, mtype, importance, content in MEMORIES:
    chash = hashlib.sha256(content.encode()).hexdigest()[:64]
    try:
        cur.execute(
            """INSERT INTO agent_memories (session_id, content, source, memory_type, importance, content_hash)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (content_hash) DO NOTHING""",
            ("claude_code_sync", content[:1000], source, mtype, importance, chash)
        )
        if cur.rowcount > 0:
            saved += 1
        else:
            skipped += 1
    except Exception as e:
        conn.rollback()
        print(f"  row failed: {e}", flush=True)
        continue

conn.commit()
cur.close()
conn.close()
print(f"Done — {saved} new, {skipped} already existed. Total attempted: {len(MEMORIES)}", flush=True)
