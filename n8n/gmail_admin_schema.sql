-- ==========================================
-- GMAIL ADMIN ASSISTANT — DATABASE SCHEMA
-- ==========================================
-- Run against the Railway PostgreSQL instance
-- Connected via DATABASE_URL environment variable
--
-- Usage: Execute this entire file against the shared Postgres DB
-- that the n8n Railway service uses.

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Core Gmail Tables ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS gmail_messages (
    id                SERIAL PRIMARY KEY,
    message_id        VARCHAR(255) NOT NULL UNIQUE,
    thread_id         VARCHAR(255) NOT NULL,
    labels            TEXT,
    sender            VARCHAR(512) NOT NULL,
    recipients        TEXT,
    subject           TEXT,
    snippet           TEXT,
    date_received     TIMESTAMP WITH TIME ZONE,
    is_unread         BOOLEAN DEFAULT TRUE,
    has_attachments   BOOLEAN DEFAULT FALSE,
    attachment_count  INTEGER DEFAULT 0,
    raw_size_bytes    INTEGER,
    raw_json          JSONB,
    processing_state  VARCHAR(50) DEFAULT 'pending',
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gmail_messages_thread_id ON gmail_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_gmail_messages_sender ON gmail_messages(sender);
CREATE INDEX IF NOT EXISTS idx_gmail_messages_created_at ON gmail_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_gmail_messages_processing_state ON gmail_messages(processing_state);
CREATE INDEX IF NOT EXISTS idx_gmail_messages_is_unread ON gmail_messages(is_unread) WHERE is_unread = TRUE;
CREATE INDEX IF NOT EXISTS idx_gmail_messages_date_received ON gmail_messages(date_received);

CREATE TABLE IF NOT EXISTS gmail_threads (
    id                SERIAL PRIMARY KEY,
    thread_id         VARCHAR(255) NOT NULL UNIQUE,
    subject           TEXT,
    participants      TEXT,
    last_message_id   VARCHAR(255),
    message_count     INTEGER DEFAULT 1,
    first_seen        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_updated      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gmail_threads_thread_id ON gmail_threads(thread_id);
CREATE INDEX IF NOT EXISTS idx_gmail_threads_last_updated ON gmail_threads(last_updated);

CREATE TABLE IF NOT EXISTS gmail_processing_state (
    id                SERIAL PRIMARY KEY,
    message_id        VARCHAR(255) NOT NULL UNIQUE,
    category          VARCHAR(50) NOT NULL,
    action            VARCHAR(50) NOT NULL,
    priority_score    INTEGER CHECK (priority_score BETWEEN 1 AND 10),
    ai_summary        TEXT,
    ai_model_used     VARCHAR(50),
    classified_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    action_executed   BOOLEAN DEFAULT FALSE,
    action_executed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_gps_category ON gmail_processing_state(category);
CREATE INDEX IF NOT EXISTS idx_gps_action ON gmail_processing_state(action);
CREATE INDEX IF NOT EXISTS idx_gps_priority ON gmail_processing_state(priority_score);
CREATE INDEX IF NOT EXISTS idx_gps_classified_at ON gmail_processing_state(classified_at);

-- ── Gmail Action Tables ───────────────────────────────────

CREATE TABLE IF NOT EXISTS gmail_actions (
    id                SERIAL PRIMARY KEY,
    message_id        VARCHAR(255) NOT NULL,
    action_type       VARCHAR(50) NOT NULL,
    details           JSONB,
    performed_by      VARCHAR(100) DEFAULT 'auto',
    correlation_id    UUID,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gmail_actions_message_id ON gmail_actions(message_id);
CREATE INDEX IF NOT EXISTS idx_gmail_actions_type ON gmail_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_gmail_actions_created_at ON gmail_actions(created_at);

CREATE TABLE IF NOT EXISTS gmail_drafts (
    id                SERIAL PRIMARY KEY,
    draft_id          VARCHAR(255),
    message_id        VARCHAR(255),
    to_address        VARCHAR(512),
    subject           TEXT,
    body_preview      TEXT,
    status            VARCHAR(30) DEFAULT 'draft',
    correlation_id    UUID,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gmail_drafts_status ON gmail_drafts(status);

CREATE TABLE IF NOT EXISTS gmail_sends (
    id                  SERIAL PRIMARY KEY,
    message_id          VARCHAR(255),
    original_message_id VARCHAR(255),
    to_address          VARCHAR(512) NOT NULL,
    subject             TEXT,
    compose_action      VARCHAR(30) NOT NULL,
    correlation_id      UUID,
    sent_at             TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gmail_sends_to_subject ON gmail_sends(to_address, subject);
CREATE INDEX IF NOT EXISTS idx_gmail_sends_sent_at ON gmail_sends(sent_at);

CREATE TABLE IF NOT EXISTS gmail_labels_applied (
    id                SERIAL PRIMARY KEY,
    message_id        VARCHAR(255) NOT NULL,
    label_name        VARCHAR(100) NOT NULL,
    applied_by        VARCHAR(100) DEFAULT 'auto_governance',
    applied_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_labels_applied_message ON gmail_labels_applied(message_id);

-- ── Calendar Cache ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS calendar_events_cache (
    id                SERIAL PRIMARY KEY,
    event_id          VARCHAR(255) NOT NULL UNIQUE,
    event_date        DATE NOT NULL,
    summary           TEXT,
    start_time        TIMESTAMP WITH TIME ZONE,
    end_time          TIMESTAMP WITH TIME ZONE,
    attendees         TEXT,
    location          TEXT,
    calendar_id       VARCHAR(100) DEFAULT 'primary',
    cached_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    cache_expires_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW() + INTERVAL '1 hour'
);

CREATE INDEX IF NOT EXISTS idx_cal_cache_date ON calendar_events_cache(event_date);
CREATE INDEX IF NOT EXISTS idx_cal_cache_expires ON calendar_events_cache(cache_expires_at);

-- ── Google Sheets Export Tracking ─────────────────────────

CREATE TABLE IF NOT EXISTS sheet_exports (
    id                SERIAL PRIMARY KEY,
    message_id        VARCHAR(255) NOT NULL,
    tab_name          VARCHAR(100) NOT NULL,
    exported_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(message_id, tab_name)
);

CREATE INDEX IF NOT EXISTS idx_sheet_exports_message_tab ON sheet_exports(message_id, tab_name);

-- ── Sender Intelligence ───────────────────────────────────

CREATE TABLE IF NOT EXISTS sender_profiles (
    id                SERIAL PRIMARY KEY,
    email             VARCHAR(512) NOT NULL UNIQUE,
    display_name      VARCHAR(255),
    domain            VARCHAR(255),
    first_seen        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_seen         TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    message_count     INTEGER DEFAULT 1,
    avg_priority      NUMERIC(3,1),
    primary_category  VARCHAR(50),
    is_vip            BOOLEAN DEFAULT FALSE,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_sender_email ON sender_profiles(email);
CREATE INDEX IF NOT EXISTS idx_sender_domain ON sender_profiles(domain);
CREATE INDEX IF NOT EXISTS idx_sender_vip ON sender_profiles(is_vip) WHERE is_vip = TRUE;

-- ── NLP Extraction ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS extracted_entities (
    id                SERIAL PRIMARY KEY,
    message_id        VARCHAR(255) NOT NULL,
    entity_type       VARCHAR(50) NOT NULL,
    entity_value      TEXT NOT NULL,
    confidence        NUMERIC(3,2),
    extracted_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entities_message ON extracted_entities(message_id);
CREATE INDEX IF NOT EXISTS idx_entities_type ON extracted_entities(entity_type);

CREATE TABLE IF NOT EXISTS action_items (
    id                SERIAL PRIMARY KEY,
    message_id        VARCHAR(255) NOT NULL,
    description       TEXT NOT NULL,
    deadline          DATE,
    status            VARCHAR(30) DEFAULT 'open',
    priority          INTEGER DEFAULT 5,
    assigned_to       VARCHAR(255),
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at      TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_action_items_status ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_action_items_deadline ON action_items(deadline) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_action_items_message ON action_items(message_id);

-- ── Workflow Operations ───────────────────────────────────

CREATE TABLE IF NOT EXISTS workflow_runs (
    id                SERIAL PRIMARY KEY,
    correlation_id    UUID DEFAULT uuid_generate_v4(),
    workflow_name     VARCHAR(100) NOT NULL,
    trigger_type      VARCHAR(30),
    trigger_source    VARCHAR(50),
    status            VARCHAR(30) DEFAULT 'running',
    started_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    finished_at       TIMESTAMP WITH TIME ZONE,
    payload_summary   TEXT,
    error_message     TEXT,
    execution_id      VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_name ON workflow_runs(workflow_name);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_started ON workflow_runs(started_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id                SERIAL PRIMARY KEY,
    entity_type       VARCHAR(50) NOT NULL,
    entity_id         VARCHAR(255) NOT NULL,
    action            VARCHAR(50) NOT NULL,
    performed_by      VARCHAR(100) DEFAULT 'system',
    details           JSONB,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);

CREATE TABLE IF NOT EXISTS errors (
    id                SERIAL PRIMARY KEY,
    workflow_name     VARCHAR(100),
    node_name         VARCHAR(200),
    error_message     TEXT,
    execution_id      VARCHAR(100),
    error_timestamp   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    raw_data          JSONB,
    resolved          BOOLEAN DEFAULT FALSE,
    resolved_at       TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_errors_workflow ON errors(workflow_name);
CREATE INDEX IF NOT EXISTS idx_errors_timestamp ON errors(error_timestamp);
CREATE INDEX IF NOT EXISTS idx_errors_unresolved ON errors(resolved) WHERE resolved = FALSE;

-- ── Auto-update trigger function ──────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE 'plpgsql';

-- Drop triggers if they exist before creating (idempotent)
DROP TRIGGER IF EXISTS update_gmail_messages_updated_at ON gmail_messages;
CREATE TRIGGER update_gmail_messages_updated_at
    BEFORE UPDATE ON gmail_messages
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_gmail_drafts_updated_at ON gmail_drafts;
CREATE TRIGGER update_gmail_drafts_updated_at
    BEFORE UPDATE ON gmail_drafts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
