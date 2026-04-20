-- 029: restore partial indexes dropped in earlier waves (R9 AUDIT-9-20)
--
-- Background
-- ----------
-- Migration 021 dropped idx_tracked_authors_name_no_s2 (a partial unique index
-- on tracked_authors(author_name) WHERE s2_author_id IS NULL) and replaced it
-- with a UNIQUE NULLS NOT DISTINCT constraint.  The constraint preserves the
-- uniqueness guarantee but the underlying system-generated index
-- (tracked_authors_name_s2_unique) is a full-table index — it cannot be used
-- for a partial scan of the IS NULL half the way the old partial index could.
--
-- Migration 028 dropped idx_papers_unread together with the is_read column —
-- that index cannot be restored (column gone); it is out of scope.
--
-- Additions
-- ---------
-- In addition to restoring the tracked_authors partial index, this migration
-- adds one more partial index that addresses a real hot-path identified during
-- the R9/R11 audit but never created:
--
--   idx_jobs_active   — used by the worker poll loop and stale-job reaper
--                       (jobs.py:383,389,332); covers the tiny subset of
--                       "live" rows so index scans stay cheap as the table
--                       grows.

BEGIN;

-- 1. Restore the query-side benefit of idx_tracked_authors_name_no_s2.
--    The UNIQUE NULLS NOT DISTINCT constraint (migration 021) already enforces
--    uniqueness; this companion partial index speeds up the IS NULL lookups
--    executed by the author-alert and author-tracking code.
CREATE INDEX IF NOT EXISTS idx_tracked_authors_name_null_s2
    ON tracked_authors (author_name)
    WHERE s2_author_id IS NULL;

-- 2. Partial index for the job worker poll loop.
--    Queries: "SELECT … FROM jobs WHERE status = 'queued' …"  and
--             "UPDATE jobs … WHERE status = 'running' …"
--    The table will accumulate many succeeded/failed rows; filtering to the
--    live statuses keeps the working set tiny.
CREATE INDEX IF NOT EXISTS idx_jobs_active
    ON jobs (created_at)
    WHERE status IN ('queued', 'running');

COMMIT;
