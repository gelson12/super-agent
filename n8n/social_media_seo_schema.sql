-- Social Media SEO Orchestrator — Database Schema
-- Run once against your Railway PostgreSQL instance

CREATE TABLE IF NOT EXISTS content_drafts (
    id              SERIAL PRIMARY KEY,
    brand           TEXT NOT NULL DEFAULT 'Bridge Digital Solution',
    brief           TEXT,
    platform        TEXT NOT NULL DEFAULT 'all',       -- instagram | facebook | tiktok | all
    ig_caption      TEXT,
    ig_hashtags     JSONB DEFAULT '[]',
    fb_caption      TEXT,
    fb_hashtags     JSONB DEFAULT '[]',
    tt_caption      TEXT,
    tt_hashtags     JSONB DEFAULT '[]',
    tt_hook         TEXT,
    image_url       TEXT,
    video_url       TEXT,
    status          TEXT NOT NULL DEFAULT 'draft',     -- draft | approved | published | failed
    optimal_times   JSONB DEFAULT '{}',
    scheduled_at    TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    ig_post_id      TEXT,
    fb_post_id      TEXT,
    tt_post_id      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS content_metrics (
    id                   SERIAL PRIMARY KEY,
    report_date          DATE NOT NULL DEFAULT CURRENT_DATE,
    ig_reach             BIGINT DEFAULT 0,
    ig_impressions       BIGINT DEFAULT 0,
    ig_followers         BIGINT DEFAULT 0,
    ig_profile_views     BIGINT DEFAULT 0,
    ig_engaged           BIGINT DEFAULT 0,
    fb_reach             BIGINT DEFAULT 0,
    fb_impressions       BIGINT DEFAULT 0,
    fb_fans              BIGINT DEFAULT 0,
    fb_page_views        BIGINT DEFAULT 0,
    fb_engaged_users     BIGINT DEFAULT 0,
    tt_followers         BIGINT DEFAULT 0,
    tt_likes             BIGINT DEFAULT 0,
    tt_videos            BIGINT DEFAULT 0,
    total_reach          BIGINT DEFAULT 0,
    total_impressions    BIGINT DEFAULT 0,
    total_followers      BIGINT DEFAULT 0,
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS content_metrics_report_date_idx ON content_metrics (report_date);

-- Indexes
CREATE INDEX IF NOT EXISTS drafts_status_scheduled_idx ON content_drafts (status, scheduled_at);
CREATE INDEX IF NOT EXISTS drafts_platform_idx ON content_drafts (platform);