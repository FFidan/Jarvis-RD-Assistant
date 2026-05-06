-- 058_job_terminal_outcomes.sql
--
-- Persist terminal result/error payloads for procrastinate-backed jobs.
--
-- The existing job_progress sidecar is already keyed by the public JARVIS
-- job UUID and joined by the jobs API/SSE bridge. Extending it keeps this
-- additive and avoids mutating Procrastinate's own schema.
--
-- No BEGIN/COMMIT — runner wraps the migration in a savepoint.

ALTER TABLE job_progress
  ADD COLUMN IF NOT EXISTS result JSONB,
  ADD COLUMN IF NOT EXISTS error JSONB;
