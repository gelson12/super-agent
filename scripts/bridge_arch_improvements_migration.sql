-- Bridge OS Architectural Improvements Migration
-- Apply via one-shot n8n webhook or psql
-- Safe to re-run (all DDL uses IF NOT EXISTS / DO $$ blocks)

-- ── 1. Outcome tracking ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bridge.outcomes (
    outcome_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memo_id            UUID REFERENCES bridge.agent_memos(memo_id) ON DELETE SET NULL,
    action_type        TEXT NOT NULL,
    target_bot         TEXT,
    lead_id            UUID,
    outcome_text       TEXT,
    success            BOOLEAN,
    measured_at        TIMESTAMPTZ DEFAULT NOW(),
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_outcomes_memo    ON bridge.outcomes(memo_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_bot     ON bridge.outcomes(target_bot);
CREATE INDEX IF NOT EXISTS idx_outcomes_lead    ON bridge.outcomes(lead_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_created ON bridge.outcomes(created_at);

-- ── 2. Lead interaction history ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bridge.lead_interactions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id          UUID NOT NULL,
    interaction_type TEXT NOT NULL,
    notes            TEXT,
    metadata_json    JSONB DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lead_interactions_lead    ON bridge.lead_interactions(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_interactions_created ON bridge.lead_interactions(created_at);

-- ── 3. Bot working memory ────────────────────────────────────────────────────
-- Rolling key-value store per bot (max 10 keys enforced in application layer)
CREATE TABLE IF NOT EXISTS bridge.bot_working_memory (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_name   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (bot_name, key)
);
CREATE INDEX IF NOT EXISTS idx_bot_wm_bot ON bridge.bot_working_memory(bot_name, updated_at DESC);

-- ── 4. Memo idempotency constraint ───────────────────────────────────────────
-- Prevents duplicate memos sent on the same calendar day with the same metadata.
-- Uses a partial index instead of a constraint because subject can be long.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'bridge'
          AND tablename  = 'agent_memos'
          AND indexname  = 'uix_memo_dedup'
    ) THEN
        CREATE UNIQUE INDEX uix_memo_dedup
        ON bridge.agent_memos (from_agent, to_agent, memo_type, subject, (created_at::date))
        WHERE status = 'open';
    END IF;
EXCEPTION WHEN OTHERS THEN
    NULL; -- silently skip if table has conflicts that prevent backfill
END $$;

-- ── 6. CEO feedback loop columns ─────────────────────────────────────────────
ALTER TABLE bridge.bot_context_overrides
    ADD COLUMN IF NOT EXISTS baseline_success_rate  FLOAT,
    ADD COLUMN IF NOT EXISTS baseline_snapshot_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_review_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS improved               BOOLEAN;

-- ── 7. CRO gate — BEFORE UPDATE coverage ─────────────────────────────────────
-- Extends existing INSERT trigger to also catch direct UPDATE to to_agent='ceo'.
CREATE OR REPLACE FUNCTION bridge.enforce_cro_gate()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.to_agent = 'ceo'
       AND NEW.memo_type IN ('approval_request', 'project_proposal', 'cro_review_complete')
       AND (
           (NEW.body_json->>'cro_score') IS NULL
           OR (NEW.body_json->>'cro_score')::integer < 70
       )
    THEN
        NEW.to_agent  := 'cro';
        NEW.memo_type := 'cro_review_request';
        NEW.body_json := NEW.body_json || jsonb_build_object(
            'auto_rerouted', true,
            'original_to',   'ceo',
            'reroute_reason','missing_or_low_cro_score'
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cro_gate ON bridge.agent_memos;
CREATE TRIGGER trg_cro_gate
BEFORE INSERT ON bridge.agent_memos
FOR EACH ROW EXECUTE FUNCTION bridge.enforce_cro_gate();

DROP TRIGGER IF EXISTS trg_cro_gate_update ON bridge.agent_memos;
CREATE TRIGGER trg_cro_gate_update
BEFORE UPDATE ON bridge.agent_memos
FOR EACH ROW
WHEN (NEW.to_agent = 'ceo' AND OLD.to_agent IS DISTINCT FROM NEW.to_agent)
EXECUTE FUNCTION bridge.enforce_cro_gate();

-- ── 8. agent_performance TTL support ─────────────────────────────────────────
ALTER TABLE bridge.agent_performance
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_agent_perf_created_at
    ON bridge.agent_performance(created_at);
