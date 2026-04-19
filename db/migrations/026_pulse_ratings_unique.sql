-- Migration 026: pulse_ratings dedup + unique constraint + user_id column
--
-- Adds user_id to pulse_ratings for forward-compatible multi-user support
-- (always NULL in single-tenant deployments), deduplicates existing rows,
-- and adds UNIQUE NULLS NOT DISTINCT (paper_id, user_id) so that double-clicks
-- are handled gracefully by ON CONFLICT DO UPDATE in the router.

-- 1. Add user_id column (nullable INTEGER, no FK — single-tenant always NULL,
--    forward-compatible for future multi-user support; no users table exists yet)
ALTER TABLE pulse_ratings
    ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- 2. Deduplicate: keep the oldest row per (paper_id, user_id) pair
DELETE FROM pulse_ratings a
USING pulse_ratings b
WHERE a.id > b.id
  AND a.paper_id = b.paper_id
  AND a.user_id IS NOT DISTINCT FROM b.user_id;

-- 3. Add unique constraint (NULLS NOT DISTINCT means two NULLs are equal,
--    so single-tenant NULL user_id rows are deduplicated per paper_id)
ALTER TABLE pulse_ratings
    ADD CONSTRAINT pulse_ratings_paper_user_uniq
    UNIQUE NULLS NOT DISTINCT (paper_id, user_id);
