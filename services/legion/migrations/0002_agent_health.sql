-- Legion P6.2 — per-agent health journal written by the background prober.
-- One row per (agent_id, model_id) pair. The prober UPSERTs on every probe
-- so the dashboard can show recent health without scanning hive_rounds.
-- Additive; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_health (
    agent_id       TEXT NOT NULL,
    model_id       TEXT NOT NULL DEFAULT '',  -- empty for agents that don't sub-route
    last_probe_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at     TIMESTAMPTZ,
    last_status    TEXT NOT NULL,             -- 'ok' | 'fail' | 'quota_exhausted' | 'rate_limited' | 'no_key' | 'disabled'
    last_error     TEXT,                       -- redacted error class / short reason
    consecutive_failures INT NOT NULL DEFAULT 0,
    latency_ms     INT,
    PRIMARY KEY (agent_id, model_id)
);

CREATE INDEX IF NOT EXISTS agent_health_status_idx
    ON agent_health (last_status, last_probe_at DESC);

COMMIT;
