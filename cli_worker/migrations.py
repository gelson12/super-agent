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


def ensure_credentials_table() -> None:
    """
    Create claude_credentials table for cross-restart token persistence.

    This is Layer 2b of the auth recovery chain — an alternative to the
    volume backup that works even if the Railway volume is unavailable.
    Unlike the Railway API approach (blocked by Cloudflare SSRF from inside
    containers), this writes directly to our PostgreSQL database which is
    always reachable from within Railway.

    Schema:
      id            — always 'primary' (only one row needed)
      credentials_b64 — base64-encoded /root/.claude/.credentials.json
      expires_at    — Unix ms timestamp extracted from credentials (0 if unknown)
      subscription_type — 'max', 'pro', etc. (empty if unknown)
      updated_at    — when this row was last written
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS claude_credentials (
                    id               VARCHAR(32) PRIMARY KEY,
                    credentials_b64  TEXT        NOT NULL,
                    expires_at       BIGINT      NOT NULL DEFAULT 0,
                    subscription_type VARCHAR(32) NOT NULL DEFAULT '',
                    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
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
