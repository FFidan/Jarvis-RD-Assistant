-- 078_paper_entities_user_scope.sql — Wave 1 Task 1.3 (H3 + M-01..M-04)
--
-- Knowledge Graph per-user scoping: add a nullable user_id column to
-- paper_entities so that the four KG read endpoints (/api/knowledge-graph,
-- /api/knowledge-graph/entities, /api/knowledge-graph/entity/{id},
-- /api/knowledge-graph/query) can filter entity↔paper rows by the calling
-- user. Without this column user B could enumerate paper IDs from user A's
-- private papers via shared entity references.
--
-- Backfill rule: stamp existing rows with the owning paper's
-- ``papers.discovered_by`` user, which the canonical-corpus design treats as
-- the single owner of each paper row (mig 042). NULL is preserved as
-- "system-shared / unattributed" per the project convention from migs
-- 062–076 — visible only to callers who are also NULL (server-to-server
-- API-key callers, admin owner path).
--
-- Primary-key decision: the existing PK is ``(paper_id, entity_id)``. Because
-- each paper has exactly one ``discovered_by`` user under the canonical
-- corpus design, there is no scenario where two ``(paper_id, entity_id,
-- user_id)`` rows for the same paper need to coexist — adding ``user_id``
-- to the PK would only create a redundant uniqueness check. We leave the PK
-- alone and rely on a sparse index for per-user lookups.
--
-- (Transaction wrapper added by the migrations runner; do not include
-- BEGIN/COMMIT here.)

ALTER TABLE paper_entities
    ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;

-- Backfill: copy the owning paper's discovered_by user into existing rows.
-- Idempotent: only updates rows where user_id is still NULL.
UPDATE paper_entities pe
   SET user_id = p.discovered_by
  FROM papers p
 WHERE pe.paper_id = p.id
   AND pe.user_id IS NULL;

-- Sparse per-user lookup index for filtered KG reads.
CREATE INDEX IF NOT EXISTS paper_entities_user_id_idx
    ON paper_entities (user_id) WHERE user_id IS NOT NULL;
