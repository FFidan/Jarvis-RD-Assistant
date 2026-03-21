-- Migration 010: Citation Graph
-- Adds paper_citations table and citations_fetched_at to papers

CREATE TABLE IF NOT EXISTS paper_citations (
    id               SERIAL PRIMARY KEY,
    source_paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    cited_paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    citation_context TEXT,
    is_influential   BOOLEAN,
    intent           TEXT[],
    fetched_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_paper_id, cited_paper_id)
);
CREATE INDEX IF NOT EXISTS idx_citations_source ON paper_citations(source_paper_id);
CREATE INDEX IF NOT EXISTS idx_citations_cited ON paper_citations(cited_paper_id);

ALTER TABLE papers ADD COLUMN IF NOT EXISTS citations_fetched_at TIMESTAMPTZ;
