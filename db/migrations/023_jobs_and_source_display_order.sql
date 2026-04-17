-- db/migrations/023_jobs_and_source_display_order.sql
BEGIN;

CREATE TABLE IF NOT EXISTS jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  progress REAL NOT NULL DEFAULT 0.0,
  progress_message TEXT,
  result JSONB,
  error JSONB,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  user_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_kind ON jobs(status, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_created_desc ON jobs(created_at DESC);

ALTER TABLE paper_sources
  ADD COLUMN IF NOT EXISTS display_order INTEGER NOT NULL DEFAULT 0;

UPDATE paper_sources SET display_order = CASE source_type
  WHEN 'local' THEN 1
  WHEN 'arxiv' THEN 2
  WHEN 'semantic_scholar' THEN 3
  WHEN 'openalex' THEN 4
  WHEN 'pubmed' THEN 5
  ELSE 99
END;

ALTER TABLE pulse_decks
  ADD COLUMN IF NOT EXISTS degraded_reason TEXT;

COMMIT;
