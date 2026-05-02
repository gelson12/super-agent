-- ============================================================
-- BRIDGE INTELLIGENCE LAYER — Database Schema
-- ============================================================
-- Run once against Railway PostgreSQL (DATABASE_URL).
-- Idempotent — safe to re-run.
-- All tables live in the existing "bridge" schema.
--
-- Tables added:
--   bridge.project_memory    — every approved/rejected/completed project
--   bridge.agent_performance — daily upsert per agent (success rate, authority)
--   bridge.cro_evaluations   — record of every CRO scoring decision
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for similarity() in memory-query
CREATE SCHEMA IF NOT EXISTS bridge;

-- ── Project Memory ────────────────────────────────────────────────────────────
-- Stores the outcome of every project that passes through the approval chain.
-- CRO and all agents query this before making decisions to leverage institutional
-- memory and auto-block proposals that have been repeatedly rejected.

CREATE TABLE IF NOT EXISTS bridge.project_memory (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_name     TEXT NOT NULL,
    project_hash     TEXT GENERATED ALWAYS AS (md5(lower(trim(project_name)))) STORED,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    agents_involved  TEXT[]  DEFAULT '{}',
    expected_roi_usd NUMERIC,
    risk_level       TEXT    DEFAULT 'medium',
    timeline_weeks   INTEGER,
    outcome          TEXT    DEFAULT 'pending',   -- pending|approved|rejected|success|failure
    cro_score        INTEGER,                      -- 0-100; NULL until CRO evaluates
    actual_revenue_usd NUMERIC,
    actual_cost_usd    NUMERIC,
    key_insights     JSONB   DEFAULT '{}',
    reusable_pattern BOOLEAN DEFAULT FALSE,
    rejection_count  INTEGER DEFAULT 0,
    memo_id          UUID
);

CREATE INDEX IF NOT EXISTS idx_project_memory_hash
    ON bridge.project_memory(project_hash);
CREATE INDEX IF NOT EXISTS idx_project_memory_outcome
    ON bridge.project_memory(outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_memory_name_trgm
    ON bridge.project_memory USING gin(project_name gin_trgm_ops);

-- ── Agent Performance ─────────────────────────────────────────────────────────
-- One row per (agent, date). Upserted after every task execution.
-- authority_level (1-10): auto-adjusted daily; <3 requires COS co-approval.

CREATE TABLE IF NOT EXISTS bridge.agent_performance (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name               TEXT    NOT NULL,
    date                     DATE    NOT NULL DEFAULT CURRENT_DATE,
    tasks_total              INTEGER DEFAULT 0,
    tasks_success            INTEGER DEFAULT 0,
    tasks_failed             INTEGER DEFAULT 0,
    decisions_made           INTEGER DEFAULT 0,
    retry_count              INTEGER DEFAULT 0,
    token_usage_est          INTEGER DEFAULT 0,
    revenue_attributed_usd   NUMERIC DEFAULT 0,
    authority_level          INTEGER DEFAULT 5,   -- 1-10
    UNIQUE (agent_name, date)
);

CREATE INDEX IF NOT EXISTS idx_agent_performance_agent_date
    ON bridge.agent_performance(agent_name, date DESC);

-- Seed default authority levels for all 10 Bridge agents
INSERT INTO bridge.agent_performance (agent_name, date, authority_level)
VALUES
    ('ceo',             CURRENT_DATE, 10),
    ('chief_of_staff',  CURRENT_DATE,  9),
    ('cso',             CURRENT_DATE,  8),
    ('cro',             CURRENT_DATE,  8),
    ('finance',         CURRENT_DATE,  7),
    ('bizdev',          CURRENT_DATE,  7),
    ('pm',              CURRENT_DATE,  6),
    ('programmer',      CURRENT_DATE,  6),
    ('researcher',      CURRENT_DATE,  5),
    ('cleaner',         CURRENT_DATE,  4)
ON CONFLICT (agent_name, date) DO NOTHING;

-- ── CRO Evaluations ───────────────────────────────────────────────────────────
-- Immutable audit log of every CRO scoring decision.
-- Referenced by the daily Meta-Intelligence report and performance dashboard.

CREATE TABLE IF NOT EXISTS bridge.cro_evaluations (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                      TIMESTAMPTZ DEFAULT NOW(),
    project_name                    TEXT,
    from_agent                      TEXT,
    memo_id                         UUID,
    cro_score                       INTEGER,          -- 0-100
    revenue_model                   TEXT,
    estimated_monthly_revenue_usd   NUMERIC,
    growth_potential                TEXT,             -- low|medium|high|exponential
    revenue_risks                   JSONB DEFAULT '[]',
    optimization_suggestions        JSONB DEFAULT '{}',
    historical_comparison           JSONB DEFAULT '{}',
    recommendation                  TEXT,             -- APPROVE|REVISE|REJECT
    next_action                     TEXT
);

CREATE INDEX IF NOT EXISTS idx_cro_evaluations_created
    ON bridge.cro_evaluations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cro_evaluations_score
    ON bridge.cro_evaluations(cro_score, recommendation);
