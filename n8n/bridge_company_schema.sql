-- ==========================================
-- BRIDGE COMPANY — DATABASE SCHEMA
-- ==========================================
-- Run against the shared Railway PostgreSQL instance (DATABASE_URL) used by
-- n8n + super-agent. Idempotent: safe to re-run.
--
-- All objects live in a dedicated "bridge" Postgres schema, isolated from the
-- default "public" schema that already contains unrelated tables (agent_memories,
-- gmail_*, workflow_runs, audit_logs, finance_transactions, and the legacy
-- bridge_leads contact-form table from bridge-digital-solution.com). Using a
-- schema namespace lets our `leads`, `website_projects`, `outreach_messages`,
-- etc. coexist without name collisions.
--
-- Access pattern in all n8n workflows: qualify every reference as
-- `bridge.<table>` (e.g. `SELECT ... FROM bridge.leads`). Do NOT rely on the
-- search_path — n8n connections usually default to `public` only.
--
-- Related plan: ~/.claude/plans/lets-call-this-project-rosy-cocoa.md
-- Workflows that read/write these tables:
--   B  bridge_researcher.json
--   C  bridge_website_builder.json
--   D  bridge_marketing.json
--   A  bridge_pm.json
--   E  bridge_billing_workflow.json

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE SCHEMA IF NOT EXISTS bridge;

-- Make the schema reachable without qualification inside this file only; all
-- runtime callers still use qualified names.
SET search_path TO bridge, public;

-- ── Campaign targets ─────────────────────────────────────
-- One row per (niche, city) combination the researcher should work.
CREATE TABLE IF NOT EXISTS bridge.campaign_targets (
    campaign_target_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    niche                 VARCHAR(80) NOT NULL,
    city                  VARCHAR(120) NOT NULL,
    region                VARCHAR(120),
    country               VARCHAR(3) NOT NULL DEFAULT 'GB',
    priority              INTEGER NOT NULL DEFAULT 5,
    active_flag           BOOLEAN NOT NULL DEFAULT TRUE,
    daily_lead_target     INTEGER NOT NULL DEFAULT 30,
    daily_website_limit   INTEGER NOT NULL DEFAULT 5,
    daily_outreach_limit  INTEGER NOT NULL DEFAULT 20,
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (niche, city, country)
);

CREATE INDEX IF NOT EXISTS idx_campaigns_active
    ON bridge.campaign_targets(active_flag, priority DESC)
    WHERE active_flag = TRUE;

-- ── System limits (key/value config knobs) ───────────────
CREATE TABLE IF NOT EXISTS bridge.system_limits (
    key          VARCHAR(80) PRIMARY KEY,
    value        TEXT NOT NULL,
    description  TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Leads (core table, one row per discovered business) ──
CREATE TABLE IF NOT EXISTS bridge.leads (
    lead_id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_name                TEXT NOT NULL,
    normalized_business_name     TEXT NOT NULL,
    category                     VARCHAR(80),
    subcategory                  VARCHAR(80),
    address_line_1               TEXT,
    address_line_2               TEXT,
    city                         VARCHAR(120),
    region                       VARCHAR(120),
    postcode                     VARCHAR(20),
    country                      VARCHAR(3) DEFAULT 'GB',
    phone_raw                    VARCHAR(50),
    phone_normalized             VARCHAR(30),
    email                        VARCHAR(255),
    email_status                 VARCHAR(20) DEFAULT 'unknown',   -- unknown | present | missing | invalid
    whatsapp_available           BOOLEAN DEFAULT FALSE,
    whatsapp_evidence_url        TEXT,
    telegram_available           BOOLEAN DEFAULT FALSE,
    telegram_handle              VARCHAR(80),
    maps_url                     TEXT,
    source_listing_url           TEXT,
    website_url_found            TEXT,
    website_domain               VARCHAR(255),
    website_presence_status      VARCHAR(40),                      -- no_website | weak_website | present | uncertain
    website_presence_confidence  INTEGER,                          -- 0..100
    website_quality_score        INTEGER,                          -- 0..100
    social_links                 JSONB,
    logo_url                     TEXT,
    business_description         TEXT,
    service_list                 JSONB,
    reviews_count                INTEGER,
    reviews_rating               NUMERIC(3,2),
    owner_contact_name           VARCHAR(200),
    opening_hours                JSONB,
    lead_score                   INTEGER,                          -- 0..100, set by qualifier
    -- 5 parallel status families (per plan §2)
    research_status              VARCHAR(40) NOT NULL DEFAULT 'New',
    website_status               VARCHAR(40) NOT NULL DEFAULT 'Queued',
    marketing_status             VARCHAR(40) NOT NULL DEFAULT 'Queued',
    finance_status               VARCHAR(40) NOT NULL DEFAULT 'N/A',
    project_status               VARCHAR(40) NOT NULL DEFAULT 'Planned',
    assigned_workflow            VARCHAR(40),
    final_outcome                VARCHAR(40),
    archived_flag                BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedup: three independent unique indexes so any of the three identifiers
-- (phone, domain, name+city) will block a duplicate insert.
CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_phone
    ON bridge.leads(phone_normalized) WHERE phone_normalized IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_domain
    ON bridge.leads(website_domain) WHERE website_domain IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_name_city
    ON bridge.leads(LOWER(normalized_business_name), LOWER(city))
    WHERE normalized_business_name IS NOT NULL AND city IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_leads_research_status
    ON bridge.leads(research_status);
CREATE INDEX IF NOT EXISTS idx_leads_website_status
    ON bridge.leads(website_status);
CREATE INDEX IF NOT EXISTS idx_leads_marketing_status
    ON bridge.leads(marketing_status);
CREATE INDEX IF NOT EXISTS idx_leads_finance_status
    ON bridge.leads(finance_status);
CREATE INDEX IF NOT EXISTS idx_leads_city_niche
    ON bridge.leads(city, category);
CREATE INDEX IF NOT EXISTS idx_leads_created_at
    ON bridge.leads(created_at DESC);

-- ── Lead sources (audit trail: where did we find each lead) ──
-- A lead can be found in multiple sources; we keep every source for audit
-- and Google Places attribution compliance (30-day display limit).
CREATE TABLE IF NOT EXISTS bridge.lead_sources (
    source_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lead_id        UUID NOT NULL REFERENCES bridge.leads(lead_id) ON DELETE CASCADE,
    source_type    VARCHAR(40) NOT NULL,        -- google_places | playwright_site | playwright_search | manual
    source_url     TEXT,
    source_ref_id  VARCHAR(200),                -- e.g. Google Places place_id
    raw_payload    JSONB,
    collected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lead_sources_lead
    ON bridge.lead_sources(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_sources_ref
    ON bridge.lead_sources(source_type, source_ref_id);

-- ── Website projects (one per demo build) ────────────────
CREATE TABLE IF NOT EXISTS bridge.website_projects (
    website_project_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lead_id             UUID NOT NULL REFERENCES bridge.leads(lead_id) ON DELETE CASCADE,
    slug                VARCHAR(200) NOT NULL UNIQUE,
    brief_json          JSONB,
    copy_json           JSONB,
    enrichment_json     JSONB,
    build_method        VARCHAR(40) DEFAULT 'static_template',
    preview_url         TEXT,
    repo_name           VARCHAR(120) DEFAULT 'bridge_websites_demos',
    repo_commit_sha     VARCHAR(80),
    deployment_status   VARCHAR(40) DEFAULT 'pending',   -- pending | live | failed | archived
    archived_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_website_projects_lead
    ON bridge.website_projects(lead_id);
CREATE INDEX IF NOT EXISTS idx_website_projects_status
    ON bridge.website_projects(deployment_status);
CREATE INDEX IF NOT EXISTS idx_website_projects_created
    ON bridge.website_projects(created_at);

-- ── Outreach messages (every send + reply) ───────────────
CREATE TABLE IF NOT EXISTS bridge.outreach_messages (
    message_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lead_id               UUID NOT NULL REFERENCES bridge.leads(lead_id) ON DELETE CASCADE,
    channel               VARCHAR(20) NOT NULL,              -- email | whatsapp | telegram
    direction             VARCHAR(10) NOT NULL,              -- outbound | inbound
    template_key          VARCHAR(80),
    subject               TEXT,
    body                  TEXT,
    provider_message_id   VARCHAR(255),
    thread_id             VARCHAR(255),
    delivery_status       VARCHAR(40),
    classification        VARCHAR(40),                       -- positive | negative | question | needs_human | out_of_office | unclear
    classification_conf   INTEGER,                           -- 0..100
    classification_summary TEXT,
    sent_at               TIMESTAMPTZ,
    received_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_outreach_lead
    ON bridge.outreach_messages(lead_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_outreach_thread
    ON bridge.outreach_messages(thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outreach_awaiting
    ON bridge.outreach_messages(channel, sent_at)
    WHERE direction = 'outbound';

-- ── Outreach templates (A/B test ready) ──────────────────
CREATE TABLE IF NOT EXISTS bridge.outreach_templates (
    template_key   VARCHAR(80) PRIMARY KEY,
    channel        VARCHAR(20) NOT NULL,
    subject_tpl    TEXT,
    body_tpl       TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    active_flag    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Suppression list (do-not-contact emails) ─────────────
CREATE TABLE IF NOT EXISTS bridge.suppression_list (
    email_hash   VARCHAR(64) PRIMARY KEY,        -- SHA-256 hex of lowercased email
    reason       VARCHAR(80),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Billing records (PayPal invoices + subscriptions) ────
CREATE TABLE IF NOT EXISTS bridge.billing_records (
    billing_record_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lead_id                 UUID NOT NULL REFERENCES bridge.leads(lead_id) ON DELETE RESTRICT,
    client_legal_name       VARCHAR(255),
    billing_email           VARCHAR(255),
    billing_address         JSONB,
    currency                VARCHAR(3) NOT NULL DEFAULT 'GBP',
    setup_fee_amount        NUMERIC(10,2),
    monthly_fee_amount      NUMERIC(10,2),
    tax_rate                NUMERIC(5,4),
    tax_amount              NUMERIC(10,2),
    invoice_total           NUMERIC(10,2),
    invoice_provider        VARCHAR(20) DEFAULT 'paypal',
    invoice_id              VARCHAR(120),
    invoice_status          VARCHAR(40),
    invoice_url             TEXT,
    invoice_sent_at         TIMESTAMPTZ,
    due_at                  TIMESTAMPTZ,
    paid_at                 TIMESTAMPTZ,
    subscription_id         VARCHAR(120),
    subscription_status     VARCHAR(40),
    refund_status           VARCHAR(40),
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_billing_lead
    ON bridge.billing_records(lead_id);
CREATE INDEX IF NOT EXISTS idx_billing_status
    ON bridge.billing_records(invoice_status);
CREATE INDEX IF NOT EXISTS idx_billing_due
    ON bridge.billing_records(due_at)
    WHERE invoice_status IN ('Invoice Sent', 'Awaiting Payment');

-- ── Tax rules (rule-based, never LLM-guessed) ────────────
-- UNIQUE NULLS NOT DISTINCT so a NULL region still collides on re-insert
-- (plain UNIQUE treats NULLs as distinct → duplicate rows across re-applies).
-- Requires PostgreSQL 15+.
CREATE TABLE IF NOT EXISTS bridge.tax_rules (
    tax_rule_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country       VARCHAR(3) NOT NULL,
    region        VARCHAR(120),
    rate          NUMERIC(5,4) NOT NULL,       -- e.g. 0.2000 for 20%
    notes         TEXT,
    active_flag   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT tax_rules_country_region_key
        UNIQUE NULLS NOT DISTINCT (country, region)
);

-- ── Expenses (internal cost tracking, per workflow run) ──
-- Every Bridge workflow logs every billable action here. Finance-Accountant
-- consolidates, raises threshold alerts, and ties per-lead COGS (Google
-- Places call + HAIKU qualification + copy LLM + etc.) against client revenue.
-- Smoke-test expenses use workflow_name='smoke_test' or the owning workflow's
-- name with a details_json.synthetic_probe=true marker.
CREATE TABLE IF NOT EXISTS bridge.expenses (
    expense_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    workflow_name  VARCHAR(40) NOT NULL,   -- researcher | website | marketing | pm | billing | smoke_test | manual
    lead_id        UUID REFERENCES bridge.leads(lead_id) ON DELETE SET NULL,
    category       VARCHAR(40) NOT NULL,   -- api_call | llm_tokens | compute | storage | payment_fee | other
    vendor         VARCHAR(40),            -- google_places | anthropic_haiku | anthropic_sonnet | deepseek | gemini | railway | paypal | github
    description    TEXT,
    units          NUMERIC(14,4),          -- e.g. 1 call, 523 input_tokens, 87 output_tokens
    unit_label     VARCHAR(40),            -- 'calls' | 'input_tokens' | 'output_tokens' | 'minutes' | 'mb'
    amount_usd     NUMERIC(10,4) NOT NULL, -- actual spend in USD
    details_json   JSONB,                  -- request_id, model, place_id, execution_id — anything searchable
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_expenses_occurred_desc
    ON bridge.expenses(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_expenses_workflow
    ON bridge.expenses(workflow_name, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_expenses_lead
    ON bridge.expenses(lead_id) WHERE lead_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_expenses_vendor
    ON bridge.expenses(vendor, occurred_at DESC);


-- ── Workflow events (append-only audit log) ──────────────
CREATE TABLE IF NOT EXISTS bridge.workflow_events (
    event_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lead_id         UUID REFERENCES bridge.leads(lead_id) ON DELETE CASCADE,
    workflow_name   VARCHAR(40) NOT NULL,          -- researcher | website | marketing | pm | billing
    event_type      VARCHAR(60) NOT NULL,          -- status_change | error | alert | handoff | ...
    old_status      VARCHAR(40),
    new_status      VARCHAR(40),
    details_json    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_lead_time
    ON bridge.workflow_events(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_workflow_time
    ON bridge.workflow_events(workflow_name, created_at DESC);

-- ── updated_at auto-refresh triggers ─────────────────────
CREATE OR REPLACE FUNCTION bridge.touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
    tables TEXT[] := ARRAY[
        'campaign_targets',
        'leads',
        'website_projects',
        'outreach_templates',
        'billing_records'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_touch ON bridge.%I;
             CREATE TRIGGER trg_%s_touch BEFORE UPDATE ON bridge.%I
               FOR EACH ROW EXECUTE FUNCTION bridge.touch_updated_at();',
            t, t, t, t
        );
    END LOOP;
END $$;

-- ==========================================
-- SEED DATA (defaults)
-- ==========================================

INSERT INTO bridge.system_limits (key, value, description) VALUES
    ('max_websites_in_progress',      '5',   'Max rows in bridge.leads with website_status IN (In Progress, Draft Ready)'),
    ('max_awaiting_reply',            '20',  'Max rows with marketing_status = Awaiting Reply'),
    ('max_daily_outreach_per_campaign', '20', 'Per-(niche,city) daily outreach cap'),
    ('min_website_presence_confidence', '70', 'Min qualifier confidence to auto-advance to Ready for Website Team'),
    ('followup_delay_hours',          '24',  'Hours after first outreach before follow-up sends'),
    ('demo_ttl_hours',                '12',  'Hours to keep a demo live if lead is not marked Interested'),
    ('default_setup_fee',             '299.00', 'Default setup/installation fee (GBP)'),
    ('default_monthly_fee',           '49.00',  'Default monthly maintenance fee (GBP)'),
    ('require_invoice_approval',      'true', 'If true, PM must manually approve each invoice before Billing sends it'),
    ('require_first_outreach_approval', 'true', 'If true, PM must approve the first outreach of any new template version')
ON CONFLICT (key) DO NOTHING;

INSERT INTO bridge.tax_rules (country, region, rate, notes) VALUES
    ('GB', NULL, 0.2000, 'UK VAT standard rate'),
    ('IE', NULL, 0.2300, 'Ireland VAT standard rate'),
    ('US', NULL, 0.0000, 'US: no federal VAT; state-level sales tax requires manual review — do NOT auto-invoice US clients without PM approval')
ON CONFLICT (country, region) DO NOTHING;

-- First campaign row (commented out — insert when ready to launch)
-- INSERT INTO bridge.campaign_targets (niche, city, country, priority, daily_lead_target)
-- VALUES ('plumber', 'Leeds', 'GB', 10, 30);

-- ==========================================
-- End of bridge_company_schema.sql
-- ==========================================
