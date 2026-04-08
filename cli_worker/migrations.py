"""
CLI Worker DB migrations — run on every boot, idempotent.

Creates the cli_tasks table if it doesn't exist, then requeues
any tasks that were left in status=running from a previous crash.
"""
import os
import psycopg2


def _conn():
    url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL not set — cli_worker requires PostgreSQL")
    return psycopg2.connect(url)


def ensure_table() -> None:
    """Create cli_tasks table and indexes if they don't exist."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cli_tasks (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    type        VARCHAR(50)  NOT NULL,
                    payload     JSONB        NOT NULL DEFAULT '{}',
                    status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
                    result      TEXT,
                    error       TEXT,
                    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    started_at  TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS cli_tasks_status_idx
                    ON cli_tasks(status);
                CREATE INDEX IF NOT EXISTS cli_tasks_created_idx
                    ON cli_tasks(created_at);
            """)
        conn.commit()


def requeue_stale_tasks() -> int:
    """
    Reset tasks stuck in 'running' for more than 3 minutes back to 'pending'.
    Called on boot — handles the crash-mid-task scenario.
    Returns the number of tasks requeued.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cli_tasks
                SET    status = 'pending',
                       started_at = NULL
                WHERE  status = 'running'
                  AND  started_at < NOW() - INTERVAL '3 minutes'
                RETURNING id
            """)
            rows = cur.fetchall()
        conn.commit()
    return len(rows)
