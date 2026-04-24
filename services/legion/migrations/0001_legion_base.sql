-- Legion Engineer P0 base schema. Additive only.
-- Safe to re-run (all CREATE IF NOT EXISTS).

BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Dual-Claude account lifecycle. Written by both inspiring-cat (account A)
-- and legion (account B). One row per account_id ('A' | 'B').
CREATE TABLE IF NOT EXISTS claude_account_state (
    account_id       TEXT PRIMARY KEY,
    container        TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('active','passive','healing','exhausted','locked')),
    token_expires_at TIMESTAMPTZ,
    last_healed_at   TIMESTAMPTZ,
    last_heartbeat   TIMESTAMPTZ NOT NULL DEFAULT now(),
    exhaustion_count INT NOT NULL DEFAULT 0,
    health_score     REAL NOT NULL DEFAULT 1.0 CHECK (health_score BETWEEN 0 AND 1),
    healing_layer    TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claude_account_state_role_hb_idx
    ON claude_account_state (role, last_heartbeat DESC);

-- Hive round journal.
CREATE TABLE IF NOT EXISTS hive_rounds (
    round_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_hash     TEXT,
    query_modality TEXT,
    agents_entered TEXT[] NOT NULL DEFAULT '{}',
    winner_agent   TEXT,
    scores_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    latency_ms     INT,
    cost_cents     REAL,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hive_rounds_ts_idx ON hive_rounds (ts DESC);
CREATE INDEX IF NOT EXISTS hive_rounds_modality_ts_idx ON hive_rounds (query_modality, ts DESC);

-- Rolling per-agent scores. EMA updates.
CREATE TABLE IF NOT EXISTS hive_agent_scores (
    agent_id         TEXT PRIMARY KEY,
    rolling_win_rate REAL NOT NULL DEFAULT 0.5,
    avg_latency_ms   REAL,
    sample_count     INT NOT NULL DEFAULT 0,
    error_rate_7d    REAL NOT NULL DEFAULT 0.0,
    last_updated     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-agent quota/rate-limit state. Either Redis or this table is authoritative.
CREATE TABLE IF NOT EXISTS agent_quota (
    agent_id       TEXT PRIMARY KEY,
    window_start   TIMESTAMPTZ NOT NULL DEFAULT now(),
    requests_used  INT NOT NULL DEFAULT 0,
    tokens_used    BIGINT NOT NULL DEFAULT 0,
    reset_at       TIMESTAMPTZ NOT NULL,
    hard_cap       INT NOT NULL,
    soft_cap       INT NOT NULL,
    circuit_state  TEXT NOT NULL DEFAULT 'closed' CHECK (circuit_state IN ('closed','half_open','open')),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed passive rows. Real tokens/emails come from Railway env vars at runtime.
INSERT INTO claude_account_state (account_id, container, role, last_heartbeat)
VALUES ('A', 'inspiring-cat', 'passive', now())
ON CONFLICT (account_id) DO NOTHING;
INSERT INTO claude_account_state (account_id, container, role, last_heartbeat)
VALUES ('B', 'legion',        'passive', now())
ON CONFLICT (account_id) DO NOTHING;

COMMIT;
