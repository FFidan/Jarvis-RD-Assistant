-- Migration 007: Add priority score to papers
-- Composite priority score (0.0-1.0) for triage badges: must-read / recommended / background.

ALTER TABLE papers ADD COLUMN IF NOT EXISTS priority_score FLOAT;
CREATE INDEX IF NOT EXISTS idx_papers_priority ON papers(priority_score DESC NULLS LAST);
