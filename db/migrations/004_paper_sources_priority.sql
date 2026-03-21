-- Migration 004: add priority column to paper_sources
-- Controls source preference when multiple sources are enabled.
-- Higher value = higher priority.

ALTER TABLE paper_sources
    ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 1;

COMMENT ON COLUMN paper_sources.priority IS
    'Fetch priority (higher = preferred). Default 1.';
