#!/usr/bin/env python3
"""
Monitoring DB migrations — idempotent, run once (or on every boot).

Creates:
  monitoring_snapshots   — full health check results per run
  monitoring_suggestions — improvement proposals awaiting human approval
"""
import os
import psycopg2


def _conn():
    url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url)


def ensure_tables() -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monitoring_snapshots (
                    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    run_type        VARCHAR(30) NOT NULL DEFAULT 'scheduled',
                    overall_status  VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    n8n_status      VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    cli_status      VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    agent_status    VARCHAR(20) NOT NULL DEFAULT 'unknown',
                    checks_passed   INT         NOT NULL DEFAULT 0,
                    checks_failed   INT         NOT NULL DEFAULT 0,
                    checks_total    INT         NOT NULL DEFAULT 0,
                    data            JSONB       NOT NULL DEFAULT '{}',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS monitoring_snapshots_created_idx
                    ON monitoring_snapshots(created_at DESC);

                CREATE TABLE IF NOT EXISTS monitoring_suggestions (
                    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    snapshot_id     UUID        REFERENCES monitoring_snapshots(id) ON DELETE SET NULL,
                    target_system   VARCHAR(100) NOT NULL,
                    severity        VARCHAR(20)  NOT NULL DEFAULT 'medium',
                    title           VARCHAR(400) NOT NULL,
                    description     TEXT         NOT NULL,
                    proposed_fix    TEXT,
                    status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
                    reviewed_by     VARCHAR(100),
                    reviewed_at     TIMESTAMPTZ,
                    applied_at      TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS monitoring_suggestions_status_idx
                    ON monitoring_suggestions(status);
                CREATE INDEX IF NOT EXISTS monitoring_suggestions_created_idx
                    ON monitoring_suggestions(created_at DESC);
            """)
        conn.commit()
    print("monitoring_snapshots and monitoring_suggestions tables ready.")


if __name__ == "__main__":
    ensure_tables()
