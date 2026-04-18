-- Migration 025: add page_number to entity_relationships (SEC-H02)
-- Stores the verified source page so KG edges can be traced back to a specific
-- page in the paper, supporting anti-hallucination auditing.

BEGIN;
ALTER TABLE entity_relationships ADD COLUMN IF NOT EXISTS page_number INTEGER;
COMMIT;
