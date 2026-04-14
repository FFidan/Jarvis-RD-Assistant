-- Migration 021: tracked_authors uniqueness over (author_name, s2_author_id) with NULLS NOT DISTINCT
-- Previously the table relied on a partial unique index that only applied when s2_author_id IS NULL.
-- That left a gap: two rows with the same author_name but non-null s2_author_id collisions were never
-- prevented at the DB level, and the create_tracked_author check used IS NOT DISTINCT FROM semantics
-- in Python but had no matching constraint. PostgreSQL 15+ supports NULLS NOT DISTINCT on unique
-- constraints, which matches the intended "two NULL s2_author_id values are equal" semantics.

BEGIN;

-- Drop the old partial unique index (name was defined in migration 008)
DROP INDEX IF EXISTS idx_tracked_authors_name_no_s2;

-- Add the replacement: one UNIQUE constraint covering both cases.
ALTER TABLE tracked_authors
  ADD CONSTRAINT tracked_authors_name_s2_unique
  UNIQUE NULLS NOT DISTINCT (author_name, s2_author_id);

COMMIT;
