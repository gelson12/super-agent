#!/bin/bash
# Read the agent_health table directly to verify the prober's writes land.
set -e
: "${PG_DSN:?PG_DSN not set}"
python3 - <<'PY'
import os, psycopg
with psycopg.connect(os.environ["PG_DSN"]) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT agent_id, last_status, last_error, latency_ms,
                   last_probe_at, last_ok_at, consecutive_failures
            FROM agent_health
            ORDER BY agent_id
        """)
        rows = cur.fetchall()
print(f"agent_health rows: {len(rows)}")
for r in rows:
    print(f"  {r[0]:14s} status={r[1]:10s} lat={r[3]} fails={r[6]} err={r[2]}")
PY
