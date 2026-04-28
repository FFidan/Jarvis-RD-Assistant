-- Migration 021: tracked_authors uniqueness over (author_name, s2_author_id) with NULLS NOT DISTINCT
-- Previously the table relied on a partial unique index that only applied when s2_author_id IS NULL.
-- That left a gap: two rows with the same author_name but non-null s2_author_id collisions were never
-- prevented at the DB level, and the create_tracked_author check used IS NOT DISTINCT FROM semantics
-- in Python but had no matching constraint. PostgreSQL 15+ supports NULLS NOT DISTINCT on unique
-- constraints, which matches the intended "two NULL s2_author_id values are equal" semantics.

BEGIN;

-- Drop the old partial unique index (name was defined in migration 008)
DROP INDEX IF EXISTS idx_tracked_authors_name_no_s2;

-- Existing databases could have duplicates because the old partial index only
-- protected the NULL s2_author_id case. Keep the oldest row per logical author.
WITH ranked AS (
  SELECT
    ctid,
    ROW_NUMBER() OVER (
      PARTITION BY author_name, s2_author_id
      ORDER BY created_at ASC NULLS LAST, id ASC
    ) AS rn
  FROM tracked_authors
)
DELETE FROM tracked_authors ta
USING ranked r
WHERE ta.ctid = r.ctid
  AND r.rn > 1;

-- Add the replacement: one UNIQUE constraint covering both cases. Fresh
-- installs already have this constraint from init.sql, so guard by name.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
    WHERE nsp.nspname = 'public'
      AND rel.relname = 'tracked_authors'
      AND con.conname = 'tracked_authors_name_s2_unique'
  ) THEN
    ALTER TABLE tracked_authors
      ADD CONSTRAINT tracked_authors_name_s2_unique
      UNIQUE NULLS NOT DISTINCT (author_name, s2_author_id);
  END IF;
END $$;

COMMIT;
