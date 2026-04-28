-- 045: functional indexes for search-preview candidate-key matching.
-- Sprint 7 B5. The candidate-key SQL added in the post-shipboot stabilization
-- predicates on lower(btrim(external_id)), (metadata->>'doi'),
-- (metadata->>'arxiv_id'), and a normalized (title, year) pair. None of the
-- existing indexes on `papers` cover those expressions, so Postgres
-- seq-scans on every preview request regardless of how few candidate keys
-- are passed. These indexes flip those predicates to index scans.

CREATE INDEX IF NOT EXISTS idx_papers_external_id_normalized
    ON papers (lower(btrim(external_id)));

CREATE INDEX IF NOT EXISTS idx_papers_metadata_doi
    ON papers ((lower(btrim(metadata->>'doi'))))
    WHERE metadata ? 'doi';

CREATE INDEX IF NOT EXISTS idx_papers_metadata_arxiv_id
    ON papers ((lower(btrim(metadata->>'arxiv_id'))))
    WHERE metadata ? 'arxiv_id';

CREATE INDEX IF NOT EXISTS idx_papers_title_year_normalized
    ON papers (
        regexp_replace(lower(btrim(title)), '[^[:alnum:]_[:space:]]', ' ', 'g'),
        EXTRACT(YEAR FROM published_date)
    )
    WHERE title IS NOT NULL AND published_date IS NOT NULL;
