-- Migration 053: Drop legacy jobs table (B.4 Step 5)
--
-- After Marathon B.4 Steps 1-4 shipped, all 19 job kinds run on
-- procrastinate (procrastinate_jobs from migration 052). The legacy
-- `jobs` table has zero production readers or writers in master.
-- This migration removes the table, its triggers, and its indexes.
--
-- Trigger/function names verified from migration 041_jobs_notify.sql:
--   trigger:  trg_jobs_notify_update  (created in 041)
--   function: notify_jobs_update()    (created in 041)
--
-- Rollback: re-create from migrations 023, 024, 029, 032, 035, 041
-- (these were the migrations that created and altered the legacy
-- `jobs` table — all of them now refer to a table that no longer
-- exists; they remain on disk for git history only).

DROP TRIGGER IF EXISTS trg_jobs_notify_update ON jobs;
DROP FUNCTION IF EXISTS notify_jobs_update();

DROP TABLE IF EXISTS jobs CASCADE;
