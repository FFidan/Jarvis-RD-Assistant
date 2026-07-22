-- 0106: Persist the source-aware paper visibility boundary.
--
-- Provenance labels are descriptive. Public visibility is backfilled only for
-- known scholarly adapters whose row did not enter through the client-driven
-- citation-batch path. Every other existing and future row defaults private.
ALTER TABLE papers
    ADD COLUMN IF NOT EXISTS visibility_scope text NOT NULL DEFAULT 'private';

DO $$ BEGIN
    ALTER TABLE papers
        ADD CONSTRAINT papers_visibility_scope_check
        CHECK (visibility_scope IN ('public', 'private'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

UPDATE papers
SET visibility_scope = 'public'
WHERE source_type IN ('arxiv', 'semantic_scholar', 'openalex', 'pubmed')
  AND discovery_origin <> 'citation_batch';

COMMENT ON COLUMN papers.visibility_scope IS
    'Server-controlled authorization scope. Public rows are shared; private rows require user_library membership.';
