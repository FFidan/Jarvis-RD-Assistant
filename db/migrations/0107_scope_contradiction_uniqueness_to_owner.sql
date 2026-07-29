-- 0107: Scope the contradiction uniqueness key to the owning user.
--
-- The evidence-pair key spanned the whole deployment, so the second user to
-- scan a shared paper pair collided with the first user's row and recorded none
-- of their own. Adding the owner widens the key: every existing row keeps its
-- identity because the key only grows, and each user can now hold their own row
-- for the same pair of quotes.
--
-- COALESCE folds legacy owner-less rows into a single bucket. A bare NULL is
-- distinct from every other NULL in a unique index, which would let unowned
-- duplicates accumulate unchecked.
DROP INDEX IF EXISTS idx_paper_contradictions_unique_quotes;

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_contradictions_unique_quotes
    ON paper_contradictions (
        LEAST(paper_a_id, paper_b_id),
        GREATEST(paper_a_id, paper_b_id),
        md5(quote_a),
        md5(quote_b),
        COALESCE(user_id, 0)
    );
