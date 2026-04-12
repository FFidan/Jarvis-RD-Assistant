-- Migration 019: Fix pdf_resolutions UNIQUE constraint to treat NULL as equal
-- (PostgreSQL 15+: NULLS NOT DISTINCT means two NULLs are considered equal in uniqueness checks)

BEGIN;

-- Drop the existing constraint (name may vary — check pg_constraint)
ALTER TABLE pdf_resolutions
  DROP CONSTRAINT IF EXISTS pdf_resolutions_doi_arxiv_id_key;

-- Re-add with NULLS NOT DISTINCT so (NULL, "2301.12345") is unique
ALTER TABLE pdf_resolutions
  ADD CONSTRAINT pdf_resolutions_doi_arxiv_id_key
  UNIQUE NULLS NOT DISTINCT (doi, arxiv_id);

COMMIT;
