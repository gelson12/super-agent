-- Bridge OS V2 Schema Extensions
-- Run against divine-contentment (bridge schema)

-- Task ledger: tracks every task_id from entry to completion
CREATE TABLE IF NOT EXISTS bridge.task_ledger (
    task_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'created',  -- created|validating|approved|executing|completed|failed|blocked
    current_agent TEXT,
    priority TEXT DEFAULT 'normal',
    requires_approval BOOLEAN DEFAULT FALSE,
    attempt_count INTEGER DEFAULT 0,
    cost_usd NUMERIC DEFAULT 0,
    revenue_usd NUMERIC DEFAULT 0,
    metadata JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_ledger_status ON bridge.task_ledger(status, created_at DESC);

-- Anomaly log: stores detected system anomalies
CREATE TABLE IF NOT EXISTS bridge.anomaly_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    anomaly_type TEXT NOT NULL,  -- cost_spike|loop_storm|agent_drift|volume_spike|repeated_failure
    severity TEXT NOT NULL,       -- critical|high|medium
    detail TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_anomaly_log_type ON bridge.anomaly_log(anomaly_type, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_log_unresolved ON bridge.anomaly_log(resolved, detected_at DESC) WHERE NOT resolved;

-- Add task_id column to agent_memos for tracking
DO $$ BEGIN
    ALTER TABLE bridge.agent_memos ADD COLUMN IF NOT EXISTS task_id TEXT REFERENCES bridge.task_ledger(task_id) ON DELETE SET NULL;
EXCEPTION WHEN others THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_agent_memos_task_id ON bridge.agent_memos(task_id) WHERE task_id IS NOT NULL;
