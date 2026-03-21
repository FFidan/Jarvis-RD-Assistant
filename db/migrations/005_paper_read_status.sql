-- Migration 005: Add read status and discovery tracking to papers
-- Supports the "What's New" feed (T0-1) by tracking which papers have been
-- seen and when they were first discovered.

ALTER TABLE papers ADD COLUMN IF NOT EXISTS is_read BOOLEAN DEFAULT FALSE;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS discovered_at TIMESTAMPTZ DEFAULT NOW();

-- Partial index for fast unread-paper queries (only indexes the FALSE rows)
CREATE INDEX IF NOT EXISTS idx_papers_unread ON papers(is_read) WHERE is_read = FALSE;

-- Back-fill: existing papers get discovered_at = created_at
UPDATE papers SET discovered_at = created_at WHERE discovered_at IS NULL;
