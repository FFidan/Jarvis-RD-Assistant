-- Phase 2 My Day: project colour / milestone columns + task colour column.
--
-- projects.color already exists in init.sql (VARCHAR(7) with hex check);
-- ADD COLUMN IF NOT EXISTS is a no-op if the column is present — safe to run
-- on both fresh installs and existing databases.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS color TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS next_milestone TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS next_milestone_due DATE;

-- tasks.color: not in init.sql, use DO/EXCEPTION guard for idempotency.
DO $$ BEGIN
  ALTER TABLE tasks ADD COLUMN color TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;
