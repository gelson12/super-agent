"""
Bridge Company — Phase 0 schema applier (autonomous).

Uses the n8n REST API (N8N_API_KEY + N8N_BASE_URL from super-agent/.env) to:
  1. Inspect what bridge_* tables already exist in the shared Postgres.
  2. If safe, apply bridge_company_schema.sql in chunks (n8n Postgres node
     has a practical per-node query-size limit; chunking keeps each under ~60 KB).
  3. Verify the 11 bridge_* tables exist with expected column counts.
  4. Delete the temporary admin workflow on success.

The temp workflow uses the shared Postgres credential id "Ae3yvuqjgMnVRrCo"
(same one every other n8n workflow uses — no credential reconnection needed).

Safe to re-run: every step uses CREATE IF NOT EXISTS / ON CONFLICT DO NOTHING.
"""

from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

import requests


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
SCHEMA_FILE = REPO_ROOT / "n8n" / "bridge_company_schema.sql"

PG_CREDENTIAL_ID = "Ae3yvuqjgMnVRrCo"
PG_CREDENTIAL_NAME = "Postgres account"
TEMP_WORKFLOW_NAME = "_BRIDGE_SCHEMA_ADMIN_TEMP"
WEBHOOK_PATH_PREFIX = "bridge-schema-admin"

EXPECTED_TABLES = [
    "campaign_targets",
    "system_limits",
    "leads",
    "lead_sources",
    "website_projects",
    "outreach_messages",
    "outreach_templates",
    "suppression_list",
    "billing_records",
    "tax_rules",
    "workflow_events",
    "expenses",
]

# Inspect tables in the `bridge` schema (post-rewrite). Also list any existing
# public.bridge_* tables for awareness — they won't collide anymore, but we log
# their presence so surprises are visible.
# Both queries aggregate to a single row (jsonb_agg) because n8n's webhook
# "lastNode" response mode appears to surface only the first item of a
# multi-row result — a single-row payload avoids any ambiguity.

INSPECT_QUERY = """
WITH src AS (
  SELECT table_schema || '.' || table_name AS full_name,
         COUNT(*)::int AS column_count,
         string_agg(column_name, ', ' ORDER BY ordinal_position) AS columns
  FROM information_schema.columns
  WHERE (table_schema = 'bridge')
     OR (table_schema = 'public' AND table_name LIKE 'bridge_%')
  GROUP BY table_schema, table_name
)
SELECT COALESCE(jsonb_agg(
         jsonb_build_object(
           'table_name', full_name,
           'column_count', column_count,
           'columns', columns
         ) ORDER BY full_name
       ), '[]'::jsonb) AS tables
FROM src;
""".strip()

VERIFY_QUERY = """
WITH expected AS (
  SELECT unnest(ARRAY[
    'campaign_targets','system_limits','leads','lead_sources',
    'outreach_messages','website_projects','outreach_templates',
    'suppression_list','billing_records','tax_rules','workflow_events',
    'expenses'
  ]) AS table_name
)
SELECT jsonb_agg(
         jsonb_build_object(
           'table_name', e.table_name,
           'column_count', COALESCE((
             SELECT COUNT(*)::int FROM information_schema.columns c
             WHERE c.table_schema='bridge' AND c.table_name=e.table_name
           ), 0)
         ) ORDER BY e.table_name
       ) AS tables,
       (SELECT COUNT(*)::int FROM bridge.system_limits) AS system_limit_rows,
       (SELECT COUNT(*)::int FROM bridge.tax_rules) AS tax_rule_rows
FROM expected e;
""".strip()


def load_env() -> dict:
    if not ENV_FILE.exists():
        sys.exit(f"[FATAL] Missing {ENV_FILE}")
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("N8N_API_KEY", "N8N_BASE_URL"):
        if k not in env:
            sys.exit(f"[FATAL] {k} missing from {ENV_FILE}")
    return env


def chunk_sql(sql: str, max_bytes: int = 50_000) -> list[str]:
    """Split SQL on statement terminators without breaking a statement.

    Simple splitter: groups whole statements into chunks up to max_bytes.
    Handles $$-quoted blocks (used by the trigger function + DO block) so we
    don't split in the middle of a dollar-quoted body.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        # toggle when we see a $$ that's not escaped; crude but sufficient for
        # our schema (no nested dollar tags, only $$ ... $$)
        if "$$" in line:
            occurrences = line.count("$$")
            for _ in range(occurrences):
                in_dollar = not in_dollar
        if not in_dollar and line.rstrip().endswith(";"):
            statements.append("".join(buf))
            buf = []
    if buf:
        statements.append("".join(buf))

    chunks: list[str] = []
    current = ""
    for stmt in statements:
        if len(current) + len(stmt) > max_bytes and current:
            chunks.append(current)
            current = stmt
        else:
            current += stmt
    if current.strip():
        chunks.append(current)
    return chunks


def build_inspect_workflow() -> dict:
    """Inspect-only: webhook -> PG Inspect. Returns current state of bridge_*."""
    webhook_path = f"{WEBHOOK_PATH_PREFIX}-inspect-{int(time.time())}"
    webhook_node = {
        "parameters": {
            "httpMethod": "GET",
            "path": webhook_path,
            "responseMode": "lastNode",
            "options": {},
        },
        "id": "wh",
        "name": "Webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [240, 300],
        "webhookId": webhook_path,
    }
    inspect_node = {
        "parameters": {
            "operation": "executeQuery",
            "query": INSPECT_QUERY,
            "options": {},
        },
        "id": "pg_inspect",
        "name": "Inspect bridge_*",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [460, 300],
        "credentials": {
            "postgres": {"id": PG_CREDENTIAL_ID, "name": PG_CREDENTIAL_NAME}
        },
    }
    return {
        "name": TEMP_WORKFLOW_NAME + "_INSPECT",
        "nodes": [webhook_node, inspect_node],
        "connections": {
            "Webhook": {"main": [[{"node": "Inspect bridge_*", "type": "main", "index": 0}]]}
        },
        "settings": {"executionOrder": "v1"},
    }


def build_apply_workflow(chunks: list[str]) -> dict:
    """Build a workflow with:
        [Webhook GET] -> [PG Apply 1..N] -> [PG Verify] -> [Respond]
    The webhook's lastNode mode returns whatever the final node emits.
    """
    nodes = []
    connections: dict[str, dict] = {}

    webhook_path = f"{WEBHOOK_PATH_PREFIX}-apply-{int(time.time())}"

    webhook_node = {
        "parameters": {
            "httpMethod": "GET",
            "path": webhook_path,
            "responseMode": "lastNode",
            "options": {},
        },
        "id": "wh",
        "name": "Webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [240, 300],
        "webhookId": webhook_path,
    }
    nodes.append(webhook_node)

    def pg_node(node_id: str, name: str, query: str, x: int):
        return {
            "parameters": {
                "operation": "executeQuery",
                "query": query,
                "options": {},
            },
            "id": node_id,
            "name": name,
            "type": "n8n-nodes-base.postgres",
            "typeVersion": 2.5,
            "position": [x, 300],
            "credentials": {
                "postgres": {"id": PG_CREDENTIAL_ID, "name": PG_CREDENTIAL_NAME}
            },
        }

    x = 460
    prev_name = "Webhook"
    for i, chunk in enumerate(chunks, start=1):
        node_name = f"Apply {i}/{len(chunks)}"
        nodes.append(pg_node(f"pg_apply_{i}", node_name, chunk, x))
        connections[prev_name] = {
            "main": [[{"node": node_name, "type": "main", "index": 0}]]
        }
        prev_name = node_name
        x += 220

    nodes.append(pg_node("pg_verify", "Verify", VERIFY_QUERY, x))
    connections[prev_name] = {
        "main": [[{"node": "Verify", "type": "main", "index": 0}]]
    }

    return {
        "name": TEMP_WORKFLOW_NAME + "_APPLY",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def n8n(env: dict, method: str, path: str, **kw):
    url = env["N8N_BASE_URL"].rstrip("/") + path
    headers = kw.pop("headers", {})
    headers.setdefault("X-N8N-API-KEY", env["N8N_API_KEY"])
    headers.setdefault("Accept", "application/json")
    if "json" in kw:
        headers.setdefault("Content-Type", "application/json")
    return requests.request(method, url, headers=headers, timeout=30, **kw)


def run_workflow(env: dict, workflow: dict, label: str) -> tuple[int, object]:
    """Create → activate → call webhook → delete. Returns (status, parsed_body)."""
    print(f"[..]   Creating {label} workflow ...")
    r = n8n(env, "POST", "/api/v1/workflows", json=workflow)
    if r.status_code >= 300:
        print(f"[FATAL] create failed {r.status_code}: {r.text[:500]}")
        return 1, None
    wf = r.json()
    wf_id = wf["id"]
    print(f"[OK]   Created id={wf_id}")

    status_code = 1
    payload: object = None
    try:
        print("[..]   Activating ...")
        r = n8n(env, "POST", f"/api/v1/workflows/{wf_id}/activate")
        if r.status_code >= 300:
            print(f"[FATAL] activate failed {r.status_code}: {r.text[:500]}")
            return 1, None

        webhook_path = workflow["nodes"][0]["parameters"]["path"]
        base = env["N8N_BASE_URL"].rstrip("/")
        webhook_url = f"{base}/webhook/{webhook_path}"
        print(f"[..]   Calling {webhook_url}")
        last_err = None
        resp = None
        for attempt in range(6):
            try:
                resp = requests.get(webhook_url, timeout=180)
                if resp.status_code < 500:
                    last_err = None
                    break
                last_err = f"{resp.status_code} {resp.text[:300]}"
            except requests.RequestException as e:
                last_err = repr(e)
            time.sleep(4)
        if last_err or resp is None:
            print(f"[FATAL] webhook call failed after retries: {last_err}")
            return 1, None

        print(f"[OK]   Webhook returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        status_code = 0 if resp.status_code < 400 else 1
    finally:
        print(f"[..]   Deleting temp workflow id={wf_id} ...")
        r = n8n(env, "DELETE", f"/api/v1/workflows/{wf_id}")
        print(f"[OK]   Cleanup {'ok' if r.status_code < 300 else 'failed ' + str(r.status_code)}")
    return status_code, payload


def format_rows(payload) -> list[dict]:
    """Normalize various n8n webhook response shapes into a list of row dicts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], list):
            return payload["data"]
        # Single-row webhook payload
        return [payload]
    return []


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "inspect").lower()
    if mode not in ("inspect", "apply"):
        print(f"Usage: {sys.argv[0]} [inspect|apply]")
        return 2

    env = load_env()

    def unpack_tables(p) -> list[dict]:
        """Both INSPECT and VERIFY queries return a single row with a `tables`
        field holding a JSONB array. n8n may surface this as an object or
        list-of-one. Handle both shapes."""
        if isinstance(p, list):
            p = p[0] if p else {}
        if isinstance(p, dict):
            tbls = p.get("tables")
            if isinstance(tbls, list):
                return tbls
        return []

    if mode == "inspect":
        print("[..]   MODE = inspect (read-only; no schema changes)")
        status, payload = run_workflow(env, build_inspect_workflow(), "inspect")
        print("\n=== Inspection result ===")
        print(json.dumps(payload, indent=2, default=str)[:6000])
        tables = unpack_tables(payload)
        existing = {t.get("table_name") for t in tables}
        in_bridge_schema = {t.split(".", 1)[1] for t in existing if t and t.startswith("bridge.")}
        in_public = sorted(t for t in existing if t and not t.startswith("bridge."))
        if in_public:
            print(f"\n[INFO] Legacy public.* bridge-named tables (untouched): {in_public}")
        if in_bridge_schema:
            print(f"\n[INFO] Tables already in `bridge` schema: {sorted(in_bridge_schema)}")
            overlap = in_bridge_schema & set(EXPECTED_TABLES)
            if overlap:
                print(f"       {len(overlap)} match our plan — a previous `apply` has run.")
                print("       Re-running `apply` is idempotent and safe.")
        else:
            print("\n[OK]   `bridge` schema is empty — safe to run `apply`.")
        return status

    # mode == "apply"
    print("[..]   MODE = apply (will CREATE IF NOT EXISTS, INSERT ON CONFLICT DO NOTHING)")
    print(f"[..]   Reading schema from {SCHEMA_FILE}")
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    chunks = chunk_sql(sql)
    print(f"[OK]   Chunked schema into {len(chunks)} Postgres node(s)")
    for i, c in enumerate(chunks, 1):
        print(f"        chunk {i}: {len(c):>6} bytes")

    workflow = build_apply_workflow(chunks)

    status, payload = run_workflow(env, workflow, "apply")
    print("\n=== Verify result ===")
    print(json.dumps(payload, indent=2, default=str)[:4000])
    tables = unpack_tables(payload)
    got = {t.get("table_name"): t.get("column_count") for t in tables}
    missing = [t for t in EXPECTED_TABLES if got.get(t, 0) == 0]
    if missing:
        print(f"\n[FAIL] Missing or empty tables in bridge schema: {missing}")
        return 1
    print(f"\n[OK]   All 12 bridge.* tables present.")
    for t in EXPECTED_TABLES:
        print(f"        bridge.{t:<25} {got.get(t)} columns")
    return status


if __name__ == "__main__":
    sys.exit(main())
