-- Cross-paper contradiction detection (anti-hallucination Layer 4).
-- Stores only quote-verified contradictions; candidates that fail quote
-- verification are discarded by the application service.

CREATE TABLE IF NOT EXISTS paper_contradictions (
    id                  SERIAL PRIMARY KEY,
    paper_a_id          INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    paper_b_id          INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    finding_a           TEXT NOT NULL,
    finding_b           TEXT NOT NULL,
    quote_a             TEXT NOT NULL,
    quote_b             TEXT NOT NULL,
    page_a              INTEGER,
    page_b              INTEGER,
    contradiction_type  VARCHAR(50) NOT NULL DEFAULT 'direct'
        CHECK (contradiction_type IN ('direct', 'methodological', 'result', 'interpretation')),
    explanation         TEXT NOT NULL,
    confidence          DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status              VARCHAR(20) NOT NULL DEFAULT 'verified'
        CHECK (status IN ('verified', 'dismissed', 'false_positive')),
    scanner_metadata    JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_paper_contradictions_distinct_papers CHECK (paper_a_id <> paper_b_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_contradictions_unique_quotes
    ON paper_contradictions (
        LEAST(paper_a_id, paper_b_id),
        GREATEST(paper_a_id, paper_b_id),
        md5(quote_a),
        md5(quote_b)
    );

CREATE INDEX IF NOT EXISTS idx_paper_contradictions_paper_a
    ON paper_contradictions (paper_a_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_contradictions_paper_b
    ON paper_contradictions (paper_b_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_contradictions_status
    ON paper_contradictions (status, created_at DESC);

COMMENT ON TABLE paper_contradictions IS
    'Verified cross-paper contradictions. Both quotes must pass QuoteVerifier before insert.';
COMMENT ON COLUMN paper_contradictions.scanner_metadata IS
    'Scanner version, candidate score, model, and other non-authoritative diagnostics.';
