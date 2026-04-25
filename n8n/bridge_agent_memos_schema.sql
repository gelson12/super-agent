-- ==========================================
-- BRIDGE COMPANY — EXECUTIVE AGENT COORDINATION
-- ==========================================
-- Adds the inter-agent memo queue used by the 5 executive super-agent bots:
--   bridge_researcher_bot, bridge_chief_of_staff_bot, bridge_chief_sec_off_bot,
--   bridge_ceo_bot, bridge_cleaner_bot.
--
-- Pattern: each bot writes typed, addressable memos to this table instead of
-- invoking other workflows directly. On its next scheduled sweep, the target
-- bot reads its inbox (WHERE to_agent = '<self>' AND status = 'open') and
-- processes. Urgent memos additionally fire an inter-agent webhook for
-- immediate handling.
--
-- Safe to re-run. Assumes bridge_company_schema.sql has already created the
-- `bridge` schema and `bridge.leads` + `bridge.system_limits` tables.
--
-- Related plan: ~/.claude/plans/proceed-calm-ember.md

SET search_path TO bridge, public;

-- ── Inter-agent memo queue ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bridge.agent_memos (
    memo_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    from_agent       TEXT NOT NULL
                     CHECK (from_agent IN
                       ('researcher','chief_of_staff','cso','ceo','cleaner','user','system')),

    -- NULL = broadcast; 'all' = explicit broadcast; otherwise a specific bot.
    to_agent         TEXT
                     CHECK (to_agent IS NULL OR to_agent IN
                       ('researcher','chief_of_staff','cso','ceo','cleaner','all')),

    memo_type        TEXT NOT NULL
                     CHECK (memo_type IN
                       ('escalation','proposal','status','directive','alert',
                        'question','decision','approval_request','approval_response')),

    priority         TEXT NOT NULL DEFAULT 'normal'
                     CHECK (priority IN ('urgent','high','normal','low')),

    subject          TEXT NOT NULL,
    body_json        JSONB NOT NULL,

    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN
                       ('open','acknowledged','in_progress','resolved','rejected','expired')),

    -- Optional link to a specific lead (nullable so non-lead memos still fit).
    related_lead_id  UUID REFERENCES bridge.leads(lead_id),

    -- Resolution audit trail.
    resolved_at      TIMESTAMPTZ,
    resolved_by      TEXT,
    resolution_notes TEXT,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Open-inbox scan is the hottest query (runs inside every cron sweep).
CREATE INDEX IF NOT EXISTS idx_agent_memos_to_status
    ON bridge.agent_memos(to_agent, priority, created_at)
    WHERE status = 'open';

-- Priority fast-path for urgent sweeps.
CREATE INDEX IF NOT EXISTS idx_agent_memos_priority_open
    ON bridge.agent_memos(priority, created_at)
    WHERE status = 'open' AND priority IN ('urgent','high');

-- Cleaner's "expired memos" scan.
CREATE INDEX IF NOT EXISTS idx_agent_memos_created
    ON bridge.agent_memos(created_at);

-- ── System-limits knobs for the 5 new bots ─────────────────────────────
-- These let the user pause individual bots via a single UPDATE without
-- redeploying workflows. Each bot's Schedule Trigger reads its flag and
-- short-circuits when 'false'.
INSERT INTO bridge.system_limits (key, value, description) VALUES
 ('researcher_bot_enabled',       'true',  'Enable Bridge_Researcher_bot cron cadences (07:30 daily, Tue 10:00 weekly)'),
 ('chief_of_staff_bot_enabled',   'true',  'Enable Bridge_Chief_Of_Staff_bot cron cadences (08:00/12:30/20:00 daily, Thu 10:00 + Fri 18:00 weekly)'),
 ('cso_bot_enabled',              'true',  'Enable Bridge_Chief_Sec_Off_bot cron cadences (08:30 daily, Wed 10:00 weekly)'),
 ('ceo_bot_enabled',              'true',  'Enable Bridge_CEO_bot cron cadences (16:00 daily, Mon 09:00 weekly)'),
 ('cleaner_bot_enabled',          'true',  'Enable Bridge_Cleaner_bot cron cadences (22:00 daily, Fri 18:00 weekly)'),
 ('exec_memo_expiry_days',        '14',    'Cleaner archives open memos older than this (then sets status=expired)'),
 ('exec_bot_default_model',       'CLAUDE','Default /chat/direct model for executive reasoning'),
 ('exec_bot_cheap_model',         'GEMINI','Default /chat/direct model for bulk / cleanup decisions')
ON CONFLICT (key) DO NOTHING;

-- ── Convenience views ──────────────────────────────────────────────────

-- Each bot's live inbox.
CREATE OR REPLACE VIEW bridge.v_agent_inbox AS
SELECT
    to_agent,
    priority,
    memo_type,
    COUNT(*)                       AS open_count,
    MIN(created_at)                AS oldest_open_at,
    MAX(created_at)                AS newest_open_at
FROM bridge.agent_memos
WHERE status = 'open'
GROUP BY to_agent, priority, memo_type;

-- Memo throughput over the last 24h (for Chief of Staff day-open briefing).
CREATE OR REPLACE VIEW bridge.v_agent_memo_24h AS
SELECT
    from_agent,
    to_agent,
    memo_type,
    priority,
    COUNT(*) AS n
FROM bridge.agent_memos
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY from_agent, to_agent, memo_type, priority
ORDER BY n DESC;
