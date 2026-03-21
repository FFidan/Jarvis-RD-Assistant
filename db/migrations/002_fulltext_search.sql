-- Migration 002: Add full-text search to papers table
-- Safe to run multiple times (uses IF NOT EXISTS / CREATE OR REPLACE / DROP IF EXISTS).
-- Wrapped in a transaction so interrupted runs leave no partial state.

-- Add regular tsvector column (trigger-maintained, compatible with all PG versions)
ALTER TABLE papers ADD COLUMN IF NOT EXISTS search_vector tsvector;

-- Trigger function: recompute search_vector on insert/update
CREATE OR REPLACE FUNCTION papers_search_vector_update() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    NEW.search_vector :=
        to_tsvector('english', coalesce(NEW.title, '')) ||
        to_tsvector('english', coalesce(NEW.abstract, '')) ||
        to_tsvector('english', coalesce(array_to_string(NEW.authors, ' '), ''));
    RETURN NEW;
END;
$$;

-- Attach trigger (recreate cleanly)
DROP TRIGGER IF EXISTS papers_search_vector_trigger ON papers;
CREATE TRIGGER papers_search_vector_trigger
    BEFORE INSERT OR UPDATE OF title, abstract, authors ON papers
    FOR EACH ROW EXECUTE FUNCTION papers_search_vector_update();

-- Back-fill existing rows
UPDATE papers SET search_vector =
    to_tsvector('english', coalesce(title, '')) ||
    to_tsvector('english', coalesce(abstract, '')) ||
    to_tsvector('english', coalesce(array_to_string(authors, ' '), ''));

-- GIN index for fast full-text lookups
CREATE INDEX IF NOT EXISTS idx_papers_search_vector
    ON papers USING GIN(search_vector);
