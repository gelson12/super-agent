"""
One-shot runner for services/legion/migrations/*.sql. Invoked via
`railway ssh --service Legion -- python -m app.run_migration`.

Uses the PG_DSN env var (set as a Railway reference to Postgres.DATABASE_URL
in the service config). All migrations are written idempotently so re-runs
are safe.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path("/app/migrations")


def main() -> int:
    dsn = os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: PG_DSN (or DATABASE_URL) not set in env", file=sys.stderr)
        return 2

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"ERROR: no migration files in {MIGRATIONS_DIR}", file=sys.stderr)
        return 2

    print(f"connecting to PG...")
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in files:
            sql = path.read_text()
            print(f"applying {path.name} ({len(sql)} bytes)...")
            conn.execute(sql)
            print(f"  ok")

        # Verify the P0 tables now exist
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE tablename IN ('claude_account_state','hive_rounds',"
                "'hive_agent_scores','agent_quota') "
                "ORDER BY tablename"
            )
            tables = [r[0] for r in cur.fetchall()]
            print(f"verified tables present: {tables}")
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
