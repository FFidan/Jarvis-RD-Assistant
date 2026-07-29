-- 0110: Require ownership for new contradiction evidence.
--
-- Historical ownerless rows carry no provenance from which an owner could be
-- inferred. Keep those rows intact while rejecting new or changed ownerless
-- evidence. NOT VALID deliberately avoids rewriting or rejecting legacy data.
DO $$
BEGIN
    ALTER TABLE paper_contradictions
        ADD CONSTRAINT chk_paper_contradictions_user_id_present
        CHECK (user_id IS NOT NULL) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

DROP INDEX IF EXISTS idx_paper_contradictions_unique_quotes;

-- New application writes normalize whitespace before insertion. Raw hashes
-- keep historical whitespace variants distinct without mutating or merging
-- their evidence.
CREATE UNIQUE INDEX idx_paper_contradictions_unique_quotes
    ON paper_contradictions (
        LEAST(paper_a_id, paper_b_id),
        GREATEST(paper_a_id, paper_b_id),
        md5(quote_a),
        md5(quote_b),
        COALESCE(user_id, 0),
        paper_a_content_generation,
        paper_b_content_generation
    );
