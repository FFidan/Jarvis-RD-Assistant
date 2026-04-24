-- Migration 035: add last_heartbeat_at column to jobs for heartbeat-based staleness detection.
-- The reaper uses COALESCE(last_heartbeat_at, started_at) so pre-migration running rows still
-- get reaped after 30 min from their started_at.  Partial index scoped to running-only.

ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_jobs_heartbeat_reaper
  ON jobs (last_heartbeat_at)
  WHERE status = 'running';
