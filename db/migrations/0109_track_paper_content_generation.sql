-- 0109: Track which version of a paper's PDF each derived artifact belongs to.
--
-- Existing papers and artifacts begin together at generation zero. Whenever a
-- verified source replaces a paper's PDF URL, the application increments the
-- paper counter in the same transaction that discards the old derived content.
-- New artifacts copy the paper's current counter so readers can distinguish
-- current evidence from retained work based on a superseded PDF.
ALTER TABLE papers
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN papers.content_generation IS
    'Monotonic version of the PDF-derived content. Incremented atomically when a verified source replacement discards that content.';

ALTER TABLE paper_highlights
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_highlights.content_generation IS
    'The paper content_generation current when this annotation was created. A mismatch means the annotation belongs to a superseded PDF.';

ALTER TABLE paper_summaries
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_summaries.content_generation IS
    'The paper content_generation summarized by this generated result.';

ALTER TABLE paper_extractions
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_extractions.content_generation IS
    'The paper content_generation used for this structured extraction.';

ALTER TABLE paper_entities
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_entities.content_generation IS
    'The paper content_generation from which this entity link was extracted.';

ALTER TABLE entity_relationships
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN entity_relationships.content_generation IS
    'The source paper content_generation supporting this relationship.';

ALTER TABLE paper_notes
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_notes.content_generation IS
    'The paper content_generation displayed when this note was created.';

ALTER TABLE cards
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN cards.content_generation IS
    'The source paper content_generation used to create this card.';

ALTER TABLE paper_contradictions
    ADD COLUMN IF NOT EXISTS paper_a_content_generation BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS paper_b_content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_contradictions.paper_a_content_generation IS
    'The paper A content_generation supporting this evidence pair.';

COMMENT ON COLUMN paper_contradictions.paper_b_content_generation IS
    'The paper B content_generation supporting this evidence pair.';
