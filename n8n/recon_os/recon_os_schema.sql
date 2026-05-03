-- ============================================================
-- RECON OS — PostgreSQL Schema
-- Authorized Penetration Testing Operations Platform
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- CLIENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS clients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    contact_name    TEXT,
    contact_email   TEXT,
    industry        TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    metadata        JSONB DEFAULT '{}'
);

-- ============================================================
-- ENGAGEMENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS engagements (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               UUID REFERENCES clients(id),
    name                    TEXT NOT NULL,
    type                    TEXT DEFAULT 'web_app',
    status                  TEXT DEFAULT 'active'
        CHECK (status IN ('active','paused','completed','archived','cancelled')),
    testing_window_start    TIMESTAMPTZ NOT NULL,
    testing_window_end      TIMESTAMPTZ NOT NULL,
    rate_limit_profile      TEXT DEFAULT 'standard'
        CHECK (rate_limit_profile IN ('standard','stealth','aggressive')),
    roe_document_url        TEXT,
    roe_hash                TEXT,
    prohibited_actions      JSONB DEFAULT '[]',
    test_accounts           JSONB DEFAULT '{}',
    emergency_contact       TEXT,
    evidence_handling_notes TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    closed_at               TIMESTAMPTZ,
    created_by              TEXT,
    lab_only                BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_engagements_client ON engagements(client_id);
CREATE INDEX IF NOT EXISTS idx_engagements_status ON engagements(status);

-- ============================================================
-- SCOPE RULES
-- ============================================================
CREATE TABLE IF NOT EXISTS scope_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id   UUID REFERENCES engagements(id) ON DELETE CASCADE,
    value           TEXT NOT NULL,
    scope_type      TEXT NOT NULL
        CHECK (scope_type IN ('domain','subdomain_wildcard','ip','cidr','web_app','api','environment','wifi_lab','subnet_lab')),
    in_scope        BOOLEAN DEFAULT TRUE,
    notes           TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    added_by        TEXT
);

CREATE INDEX IF NOT EXISTS idx_scope_rules_engagement ON scope_rules(engagement_id, in_scope);

-- ============================================================
-- PROHIBITED ACTIONS
-- ============================================================
CREATE TABLE IF NOT EXISTS prohibited_actions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id       UUID REFERENCES engagements(id) ON DELETE CASCADE,
    description         TEXT NOT NULL,
    action_type         TEXT,
    requires_approval   BOOLEAN DEFAULT FALSE
);

-- ============================================================
-- ASSETS
-- ============================================================
CREATE TABLE IF NOT EXISTS assets (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id       UUID REFERENCES engagements(id) ON DELETE CASCADE,
    asset_value         TEXT NOT NULL,
    asset_type          TEXT NOT NULL
        CHECK (asset_type IN ('domain','subdomain','ip','web_app','api','cloud_resource','service')),
    scope_status        TEXT DEFAULT 'in_scope'
        CHECK (scope_status IN ('in_scope','out_of_scope','pending_verification')),
    owner_team          TEXT,
    business_criticality TEXT DEFAULT 'medium'
        CHECK (business_criticality IN ('critical','high','medium','low')),
    environment         TEXT DEFAULT 'unknown'
        CHECK (environment IN ('production','staging','development','unknown')),
    tech_stack          TEXT[],
    tls_version         TEXT,
    cert_expiry         DATE,
    http_status         INT,
    auth_required       BOOLEAN DEFAULT FALSE,
    last_discovered     TIMESTAMPTZ DEFAULT NOW(),
    last_tested         TIMESTAMPTZ,
    screenshot_url      TEXT,
    notes               TEXT,
    metadata            JSONB DEFAULT '{}',
    UNIQUE(engagement_id, asset_value)
);

CREATE INDEX IF NOT EXISTS idx_assets_engagement ON assets(engagement_id);
CREATE INDEX IF NOT EXISTS idx_assets_scope ON assets(engagement_id, scope_status);
CREATE INDEX IF NOT EXISTS idx_assets_last_tested ON assets(last_tested);

-- ============================================================
-- DISCOVERY RECORDS
-- ============================================================
CREATE TABLE IF NOT EXISTS discovery_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id   UUID REFERENCES engagements(id),
    asset_id        UUID REFERENCES assets(id),
    source          TEXT,
    record_type     TEXT,
    record_value    TEXT,
    raw_data        JSONB DEFAULT '{}',
    discovered_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovery_asset ON discovery_records(asset_id);

-- ============================================================
-- SCAN JOBS
-- ============================================================
CREATE TABLE IF NOT EXISTS scan_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id   UUID REFERENCES engagements(id),
    asset_id        UUID REFERENCES assets(id),
    tool            TEXT NOT NULL,
    profile         TEXT,
    status          TEXT DEFAULT 'pending'
        CHECK (status IN ('pending','running','completed','failed','cancelled')),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,
    raw_output_ref  TEXT,
    module_type     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scan_jobs_engagement ON scan_jobs(engagement_id, status);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_module ON scan_jobs(module_type);

-- ============================================================
-- RAW FINDINGS
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id   UUID REFERENCES engagements(id),
    asset_id        UUID REFERENCES assets(id),
    scan_job_id     UUID REFERENCES scan_jobs(id),
    source_tool     TEXT NOT NULL,
    title           TEXT NOT NULL,
    raw_severity    TEXT,
    raw_evidence    TEXT,
    raw_data        JSONB DEFAULT '{}',
    status          TEXT DEFAULT 'pending_dedup'
        CHECK (status IN ('pending_dedup','deduplicated','merged')),
    ingested_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_raw_findings_engagement ON raw_findings(engagement_id, status);

-- ============================================================
-- NORMALIZED FINDINGS
-- ============================================================
CREATE TABLE IF NOT EXISTS normalized_findings (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id           UUID REFERENCES engagements(id),
    asset_id                UUID REFERENCES assets(id),
    raw_finding_ids         UUID[] DEFAULT '{}',
    title                   TEXT NOT NULL,
    category                TEXT,
    cwe_id                  TEXT,
    cve_id                  TEXT,
    owasp_category          TEXT,
    severity                TEXT DEFAULT 'medium'
        CHECK (severity IN ('critical','high','medium','low','informational')),
    recon_score             NUMERIC(4,2) DEFAULT 0,
    priority_bucket         TEXT DEFAULT 'medium_priority',
    score_explanation       TEXT,
    validation_status       TEXT DEFAULT 'pending_analyst'
        CHECK (validation_status IN (
            'pending_analyst','analyst_reviewed','validated',
            'likely_false_positive','verified_fixed'
        )),
    validation_notes        TEXT,
    validated_by            TEXT,
    validated_at            TIMESTAMPTZ,
    remediation_status      TEXT DEFAULT 'open'
        CHECK (remediation_status IN (
            'open','in_progress','fixed_pending_retest',
            'partially_fixed','verified_fixed','reopened','accepted_risk'
        )),
    remediation_owner       TEXT,
    remediation_evidence    TEXT,
    remediation_notes       TEXT,
    remediation_updated_at  TIMESTAMPTZ,
    business_impact         TEXT,
    remediation_guidance    TEXT,
    ext_references          TEXT[] DEFAULT '{}',
    evidence_summary        TEXT,
    dedup_fingerprint       TEXT,
    occurrence_count        INT DEFAULT 1,
    is_recurring            BOOLEAN DEFAULT FALSE,
    recurrence_count        INT DEFAULT 0,
    first_seen              TIMESTAMPTZ DEFAULT NOW(),
    last_seen               TIMESTAMPTZ DEFAULT NOW(),
    closed_at               TIMESTAMPTZ,
    reopen_count            INT DEFAULT 0,
    analyst_notes           TEXT,
    metadata                JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_nf_engagement ON normalized_findings(engagement_id);
CREATE INDEX IF NOT EXISTS idx_nf_status ON normalized_findings(validation_status, remediation_status);
CREATE INDEX IF NOT EXISTS idx_nf_score ON normalized_findings(recon_score DESC);
CREATE INDEX IF NOT EXISTS idx_nf_dedup ON normalized_findings(dedup_fingerprint, engagement_id);
CREATE INDEX IF NOT EXISTS idx_nf_bucket ON normalized_findings(priority_bucket, engagement_id);

-- ============================================================
-- EVIDENCE REFERENCES
-- ============================================================
CREATE TABLE IF NOT EXISTS evidence_references (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id      UUID REFERENCES normalized_findings(id) ON DELETE CASCADE,
    evidence_type   TEXT
        CHECK (evidence_type IN (
            'screenshot','http_request','http_response',
            'log_excerpt','tool_output','analyst_note'
        )),
    title           TEXT,
    content         TEXT,
    storage_ref     TEXT,
    collected_at    TIMESTAMPTZ DEFAULT NOW(),
    collected_by    TEXT,
    is_sensitive    BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_evidence_finding ON evidence_references(finding_id);

-- ============================================================
-- VALIDATION HISTORY
-- ============================================================
CREATE TABLE IF NOT EXISTS validation_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id      UUID REFERENCES normalized_findings(id),
    previous_status TEXT,
    new_status      TEXT,
    changed_by      TEXT,
    changed_at      TIMESTAMPTZ DEFAULT NOW(),
    notes           TEXT
);

-- ============================================================
-- RISK SCORE HISTORY
-- ============================================================
CREATE TABLE IF NOT EXISTS risk_score_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id      UUID REFERENCES normalized_findings(id),
    recon_score     NUMERIC(4,2),
    priority_bucket TEXT,
    score_breakdown JSONB DEFAULT '{}',
    scored_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- REPORTS
-- ============================================================
CREATE TABLE IF NOT EXISTS reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id   UUID REFERENCES engagements(id),
    report_type     TEXT CHECK (report_type IN ('technical','executive','remediation')),
    status          TEXT DEFAULT 'draft'
        CHECK (status IN ('draft','pending_approval','approved','delivered')),
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    delivered_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    metadata        JSONB DEFAULT '{}'
);

-- ============================================================
-- REPORT SECTIONS
-- ============================================================
CREATE TABLE IF NOT EXISTS report_sections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id       UUID REFERENCES reports(id) ON DELETE CASCADE,
    finding_id      UUID REFERENCES normalized_findings(id),
    section_type    TEXT,
    content         JSONB DEFAULT '{}',
    sort_order      INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- REMEDIATION TICKETS
-- ============================================================
CREATE TABLE IF NOT EXISTS remediation_tickets (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id          UUID REFERENCES normalized_findings(id),
    engagement_id       UUID REFERENCES engagements(id),
    owner               TEXT,
    due_date            DATE,
    status              TEXT DEFAULT 'open',
    external_ticket_id  TEXT,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- RETEST RECORDS
-- ============================================================
CREATE TABLE IF NOT EXISTS retest_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id      UUID REFERENCES normalized_findings(id),
    engagement_id   UUID REFERENCES engagements(id),
    scheduled_at    TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    result          TEXT CHECK (result IN ('pass','fail','partial','cancelled')),
    retest_evidence TEXT,
    retested_by     TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- WEAKNESS PATTERNS (cross-engagement memory)
-- ============================================================
CREATE TABLE IF NOT EXISTS weakness_patterns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID REFERENCES clients(id),
    category            TEXT,
    cwe_id              TEXT,
    owasp_category      TEXT,
    occurrence_count    INT DEFAULT 1,
    engagement_count    INT DEFAULT 1,
    first_seen          TIMESTAMPTZ DEFAULT NOW(),
    last_seen           TIMESTAMPTZ DEFAULT NOW(),
    pattern_type        TEXT DEFAULT 'recurring'
        CHECK (pattern_type IN ('systemic','recurring','emerging','resolved')),
    narrative           TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(client_id, cwe_id)
);

-- ============================================================
-- FINDING TEMPLATE LIBRARY
-- ============================================================
CREATE TABLE IF NOT EXISTS finding_templates (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title                   TEXT NOT NULL,
    category                TEXT,
    cwe_id                  TEXT,
    owasp_category          TEXT,
    default_severity        TEXT,
    summary_template        TEXT,
    impact_template         TEXT,
    remediation_template    TEXT,
    ext_references          TEXT[] DEFAULT '{}',
    created_by              TEXT DEFAULT 'system',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    is_active               BOOLEAN DEFAULT TRUE
);

-- ============================================================
-- AUDIT LOGS (append-only — never UPDATE or DELETE)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      TEXT NOT NULL,
    actor           TEXT DEFAULT 'system',
    engagement_id   UUID,
    entity_type     TEXT,
    entity_id       UUID,
    payload         JSONB DEFAULT '{}',
    ip_address      INET,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_engagement ON audit_logs(engagement_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_logs(event_type, created_at DESC);

-- ============================================================
-- ENGAGEMENT METRICS
-- ============================================================
CREATE TABLE IF NOT EXISTS engagement_metrics (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id           UUID REFERENCES engagements(id) UNIQUE,
    total_assets            INT DEFAULT 0,
    assets_tested           INT DEFAULT 0,
    total_findings          INT DEFAULT 0,
    validated_findings      INT DEFAULT 0,
    false_positives         INT DEFAULT 0,
    verified_fixed          INT DEFAULT 0,
    avg_mttr_days           NUMERIC(6,2),
    coverage_pct            NUMERIC(5,2),
    criticals               INT DEFAULT 0,
    highs                   INT DEFAULT 0,
    mediums                 INT DEFAULT 0,
    lows                    INT DEFAULT 0,
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- WORKFLOW RUN LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS workflow_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_name   TEXT NOT NULL,
    engagement_id   UUID,
    status          TEXT CHECK (status IN ('success','failed','partial')),
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,
    metadata        JSONB DEFAULT '{}'
);

-- ============================================================
-- SEED: FINDING TEMPLATE LIBRARY
-- ============================================================
INSERT INTO finding_templates (title, category, cwe_id, owasp_category, default_severity, summary_template, impact_template, remediation_template, references) VALUES
('Missing Content-Security-Policy Header', 'config', 'CWE-693', 'A05:2021', 'medium',
 'The application does not set a Content-Security-Policy header, leaving it vulnerable to XSS and data injection attacks.',
 'An attacker may be able to inject and execute malicious scripts in users'' browsers, potentially stealing session tokens or credentials.',
 'Configure a strict Content-Security-Policy header on all HTTP responses. Start with a report-only policy and tighten over time.',
 ARRAY['https://developer.mozilla.org/en-US/docs/Web/HTTP/CSP', 'https://owasp.org/www-project-secure-headers/']),

('Missing Strict-Transport-Security Header', 'config', 'CWE-319', 'A02:2021', 'medium',
 'The application does not enforce HSTS, allowing downgrade attacks from HTTPS to HTTP.',
 'An attacker with network access could strip TLS and intercept traffic in plaintext.',
 'Add the Strict-Transport-Security header with a max-age of at least 31536000 and includeSubDomains.',
 ARRAY['https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Strict-Transport-Security']),

('TLS 1.0 or 1.1 Supported', 'crypto', 'CWE-326', 'A02:2021', 'high',
 'The server accepts connections using deprecated TLS 1.0 or TLS 1.1 protocols which have known weaknesses.',
 'Attackers may exploit protocol weaknesses (BEAST, POODLE) to decrypt encrypted traffic.',
 'Disable TLS 1.0 and 1.1. Configure the server to accept only TLS 1.2 and TLS 1.3.',
 ARRAY['https://cve.mitre.org/cgi-bin/cvekey.cgi?keyword=TLS+1.0']),

('SSL Certificate Expiring Soon', 'crypto', 'CWE-298', 'A02:2021', 'medium',
 'The SSL/TLS certificate for this asset expires within 30 days.',
 'Certificate expiry causes browser trust errors and service outages.',
 'Renew the certificate before expiry. Consider automated certificate management (Let''s Encrypt, ACME).',
 ARRAY['https://letsencrypt.org/docs/']),

('Exposed Admin Panel', 'exposure', 'CWE-284', 'A01:2021', 'high',
 'An administrative or management panel is accessible without prior authentication requirements being enforced at the network level.',
 'Attackers can target admin panels with brute force or credential stuffing to gain privileged access.',
 'Restrict admin panel access to authorized IP ranges. Require MFA for all admin accounts.',
 ARRAY['https://owasp.org/www-project-web-security-testing-guide/']),

('Missing X-Frame-Options Header', 'config', 'CWE-1021', 'A05:2021', 'low',
 'The application does not set X-Frame-Options or frame-ancestors CSP directive, potentially enabling clickjacking.',
 'An attacker could embed the application in an iframe and trick users into performing unintended actions.',
 'Add X-Frame-Options: DENY or SAMEORIGIN header, or configure frame-ancestors in CSP.',
 ARRAY['https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-Frame-Options']),

('Missing X-Content-Type-Options Header', 'config', 'CWE-693', 'A05:2021', 'low',
 'The application does not set X-Content-Type-Options: nosniff, allowing MIME type sniffing.',
 'Browsers may misinterpret response content types, enabling cross-site scripting in some scenarios.',
 'Add X-Content-Type-Options: nosniff to all HTTP responses.',
 ARRAY['https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-Content-Type-Options']),

('Server Version Disclosure', 'exposure', 'CWE-200', 'A05:2021', 'informational',
 'The server discloses its software version in HTTP response headers (e.g., Server: nginx/1.18.0).',
 'Version disclosure assists attackers in targeting known vulnerabilities for the specific software version.',
 'Configure the server to suppress or genericize version information in response headers.',
 ARRAY['https://owasp.org/www-project-web-security-testing-guide/'])

ON CONFLICT DO NOTHING;

-- ============================================================
-- REVOKE DELETE/UPDATE ON audit_logs for app role
-- (uncomment and configure for production)
-- ============================================================
-- REVOKE UPDATE, DELETE ON audit_logs FROM recon_os_app;
-- GRANT INSERT, SELECT ON audit_logs TO recon_os_app;

COMMENT ON TABLE audit_logs IS 'Append-only audit trail. Never UPDATE or DELETE rows.';
COMMENT ON TABLE normalized_findings IS 'Deduplicated, scored, and validated findings. Single source of truth for all reporting.';
COMMENT ON TABLE scope_rules IS 'Hard scope enforcement table. All testing must validate against this before proceeding.';
