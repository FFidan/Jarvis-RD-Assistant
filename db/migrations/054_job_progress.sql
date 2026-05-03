-- 054_job_progress.sql
--
-- Persistent progress storage for procrastinate-backed jobs.
--
-- Procrastinate's own ``procrastinate_jobs`` table tracks status (todo / doing /
-- succeeded / failed / cancelled) but NOT the per-job progress percentage and
-- human-readable message that the legacy SSE bridge surfaces to the dashboard.
-- The ``ProcrastinateJobContextShim`` (see
-- ``libs/jarvis_common/jarvis_common/_ctx_shim.py``) UPSERTs into this table
-- every time a handler calls ``ctx.update_progress(...)``.
--
-- ``stream_job_events`` (libs/jarvis_common/jarvis_common/jobs.py) LEFT JOINs
-- this table when polling ``procrastinate_jobs`` so SSE frames carry both the
-- procrastinate status and the latest progress snapshot.
--
-- Primary key is ``jarvis_job_id`` (TEXT) so the row is keyed by the JARVIS
-- UUID stored in ``procrastinate_jobs.args->>'job_id'`` rather than the
-- procrastinate bigint id — that way the row survives retries and stays
-- reachable from API code that only knows the JARVIS job UUID.
--
-- No BEGIN/COMMIT — runner wraps the migration in a savepoint per W1-1 lint
-- contract (see scripts/check-migrations-no-tx.sh).

CREATE TABLE IF NOT EXISTS job_progress (
  jarvis_job_id TEXT PRIMARY KEY,
  progress      REAL NOT NULL DEFAULT 0,
  message       TEXT,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_job_progress_updated_at ON job_progress(updated_at);
