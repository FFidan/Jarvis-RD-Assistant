-- Migration 008: TLDR summaries + Author tracking and alerts

-- Part 1: TLDR column
ALTER TABLE paper_summaries ADD COLUMN IF NOT EXISTS tldr TEXT;
COMMENT ON COLUMN paper_summaries.tldr IS
    'One-sentence summary (max 30 words). Source: S2 TLDR API or LLM-generated.';

-- Part 2: Author tracking tables
CREATE TABLE IF NOT EXISTS tracked_authors (
    id              SERIAL PRIMARY KEY,
    author_name     TEXT NOT NULL,
    s2_author_id    VARCHAR(50),
    source          VARCHAR(20) NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'auto_starred', 'auto_rated')),
    enabled         BOOLEAN DEFAULT TRUE,
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (author_name, s2_author_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_authors_enabled
    ON tracked_authors(enabled) WHERE enabled = TRUE;
-- NULL-safe unique: prevent duplicate (author_name) when s2_author_id IS NULL
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_authors_name_no_s2
    ON tracked_authors (author_name) WHERE s2_author_id IS NULL;

CREATE TABLE IF NOT EXISTS author_alert_log (
    id                SERIAL PRIMARY KEY,
    tracked_author_id INTEGER REFERENCES tracked_authors(id) ON DELETE CASCADE,
    paper_id          INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    notified_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tracked_author_id, paper_id)
);

-- Widen nudge_type CHECK to include author_alert
ALTER TABLE scheduled_nudges DROP CONSTRAINT IF EXISTS scheduled_nudges_nudge_type_check;
ALTER TABLE scheduled_nudges ADD CONSTRAINT scheduled_nudges_nudge_type_check
    CHECK (nudge_type IN (
        'deadline_warning','daily_summary','review_reminder',
        'paper_digest','research_pulse','author_alert'
    ));

INSERT INTO scheduled_nudges (nudge_type, cron_expression, enabled)
VALUES ('author_alert', '0 10 * * *', TRUE) ON CONFLICT DO NOTHING;
