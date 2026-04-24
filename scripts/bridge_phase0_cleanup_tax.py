"""One-off cleanup: fix duplicate rows in bridge.tax_rules and tighten its
UNIQUE constraint so NULL regions collide on re-insert. Runs via the same
n8n-webhook pattern as bridge_phase0_apply_schema.py so no direct DB access
is needed.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_phase0_apply_schema import (  # type: ignore
    load_env, run_workflow, PG_CREDENTIAL_ID, PG_CREDENTIAL_NAME,
)

CLEANUP_SQL = """
-- 1. Check Postgres version (returned but not acted on)
-- 2. Drop old UNIQUE (country, region) which treats NULLs as distinct
ALTER TABLE bridge.tax_rules
    DROP CONSTRAINT IF EXISTS tax_rules_country_region_key;
-- 3. Remove exact duplicates keeping one per (country, COALESCE(region,''))
DELETE FROM bridge.tax_rules a
    USING bridge.tax_rules b
    WHERE a.ctid < b.ctid
      AND a.country = b.country
      AND COALESCE(a.region,'') = COALESCE(b.region,'');
-- 4. Recreate the constraint with NULLS NOT DISTINCT (PG 15+). If that fails,
--    fall back to partial indexes inside a DO block.
DO $$
BEGIN
    BEGIN
        EXECUTE 'ALTER TABLE bridge.tax_rules
                   ADD CONSTRAINT tax_rules_country_region_key
                   UNIQUE NULLS NOT DISTINCT (country, region)';
    EXCEPTION WHEN syntax_error OR feature_not_supported THEN
        EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS uq_tax_rules_country_nullregion
                   ON bridge.tax_rules(country) WHERE region IS NULL';
        EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS uq_tax_rules_country_region
                   ON bridge.tax_rules(country, region) WHERE region IS NOT NULL';
    END;
END $$;
SELECT
  (SELECT COUNT(*)::int FROM bridge.tax_rules) AS remaining_rows,
  (SELECT version()) AS pg_version;
""".strip()


def build_workflow() -> dict:
    webhook_path = "bridge-tax-cleanup"
    return {
        "name": "_BRIDGE_TAX_CLEANUP_TEMP",
        "nodes": [
            {
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
            },
            {
                "parameters": {
                    "operation": "executeQuery",
                    "query": CLEANUP_SQL,
                    "options": {},
                },
                "id": "pg",
                "name": "Cleanup",
                "type": "n8n-nodes-base.postgres",
                "typeVersion": 2.5,
                "position": [460, 300],
                "credentials": {
                    "postgres": {"id": PG_CREDENTIAL_ID, "name": PG_CREDENTIAL_NAME}
                },
            },
        ],
        "connections": {
            "Webhook": {"main": [[{"node": "Cleanup", "type": "main", "index": 0}]]}
        },
        "settings": {"executionOrder": "v1"},
    }


def main() -> int:
    env = load_env()
    status, payload = run_workflow(env, build_workflow(), "tax-cleanup")
    print("\n=== Result ===")
    print(json.dumps(payload, indent=2, default=str)[:2000])
    return status


if __name__ == "__main__":
    sys.exit(main())
