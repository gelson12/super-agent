"""
Adds smart database volume maintenance to the Cleaner bot:
1. Appends DB maintenance instructions to the system prompt
2. Adds a "Weekly DB cleanup" Postgres node that runs direct SQL cleanup
   every Sunday — no LLM involved, fast and reliable
"""
import json
import re
import uuid

N8N_DIR = __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))), "n8n")

DB_MAINTENANCE_ADDITION = (
    "\n\nDATABASE VOLUME MAINTENANCE (weekly — every Sunday 02:00 UTC):\n"
    "You are responsible for preventing the postgres-volume from filling up.\n"
    "The volume limit is 10GB. At 80% usage you MUST clean. At 90% escalate to CEO.\n\n"
    "AUTOMATIC SAFE CLEANUP (run weekly without approval):\n"
    "1. Delete resolved memos older than 30 days\n"
    "2. Delete workflow events older than 60 days\n"
    "3. Delete agent_performance rows older than 90 days\n"
    "4. Delete cro_evaluations older than 90 days\n"
    "5. Check DB size — escalate to CEO if > 8.0 GB (80% of 10GB limit)\n\n"
    "Report cleanup stats in reply_text: rows deleted per table, current DB size after cleanup.\n"
    "ESCALATE TO CEO immediately if DB size exceeds 8 GB."
)

CLEANUP_SQL = """WITH del_memos AS (
  DELETE FROM bridge.agent_memos
  WHERE status = 'resolved' AND resolved_at < NOW() - INTERVAL '30 days'
  RETURNING 1
),
del_events AS (
  DELETE FROM bridge.workflow_events
  WHERE created_at < NOW() - INTERVAL '60 days'
  RETURNING 1
),
del_perf AS (
  DELETE FROM bridge.agent_performance
  WHERE date < CURRENT_DATE - INTERVAL '90 days'
  RETURNING 1
),
del_cro AS (
  DELETE FROM bridge.cro_evaluations
  WHERE created_at < NOW() - INTERVAL '90 days'
  RETURNING 1
),
size_check AS (
  SELECT
    pg_size_pretty(pg_database_size(current_database())) AS db_size,
    ROUND(pg_database_size(current_database()) / 1024.0 / 1024.0 / 1024.0, 2) AS gb_used
)
SELECT
  (SELECT COUNT(*) FROM del_memos)  AS memos_deleted,
  (SELECT COUNT(*) FROM del_events) AS events_deleted,
  (SELECT COUNT(*) FROM del_perf)   AS perf_deleted,
  (SELECT COUNT(*) FROM del_cro)    AS cro_deleted,
  db_size, gb_used
FROM size_check;"""

with open(f"{N8N_DIR}/bridge_cleaner_bot.json", encoding="utf-8") as f:
    d = json.load(f)

# 1. Patch system prompt
for n in d["nodes"]:
    if n["name"] == "Assemble prompt":
        code = n["parameters"]["jsCode"]
        # Find the system string — ends with \n"; before \n\nconst contextBlock
        # Safe approach: find last occurrence of \\n"; which closes the system string
        close_marker = '\\n";'
        idx = code.rfind(close_marker, 0, code.find("const contextBlock"))
        if idx != -1 and "DATABASE VOLUME MAINTENANCE" not in code:
            # Inject before the closing
            escaped = DB_MAINTENANCE_ADDITION.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            code = code[:idx] + escaped + code[idx:]
            n["parameters"]["jsCode"] = code
            print("System prompt updated")
        break

# 2. Add Weekly DB cleanup Postgres node (direct SQL, bypasses LLM)
if not any(n.get("name") == "Weekly DB cleanup" for n in d["nodes"]):
    weekly_pos = next(
        (n.get("position", [240, 300]) for n in d["nodes"] if n["name"] == "Schedule: weekly"),
        [240, 480]
    )
    cleanup_node = {
        "id": str(uuid.uuid4()),
        "name": "Weekly DB cleanup",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2,
        "position": [weekly_pos[0] + 240, weekly_pos[1] + 160],
        "parameters": {
            "operation": "executeQuery",
            "query": CLEANUP_SQL,
        },
        "credentials": {"postgres": {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}},
    }
    d["nodes"].append(cleanup_node)

    # Wire Schedule: weekly → Weekly DB cleanup (add to existing connections)
    conns = d.get("connections", {})
    weekly_conn = conns.get("Schedule: weekly", {})
    main_outputs = weekly_conn.get("main", [[]])
    if main_outputs:
        main_outputs[0].append({"node": "Weekly DB cleanup", "type": "main", "index": 0})
    else:
        weekly_conn["main"] = [[{"node": "Weekly DB cleanup", "type": "main", "index": 0}]]
    conns["Schedule: weekly"] = weekly_conn
    d["connections"] = conns
    print(f"Added Weekly DB cleanup node: {cleanup_node['id']}")

with open(f"{N8N_DIR}/bridge_cleaner_bot.json", "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
print("Saved bridge_cleaner_bot.json")
