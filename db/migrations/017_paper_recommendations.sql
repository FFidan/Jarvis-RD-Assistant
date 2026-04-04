CREATE TABLE IF NOT EXISTS paper_recommendations (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    score           FLOAT NOT NULL,
    modes           TEXT[] NOT NULL DEFAULT '{}',
    explanation     TEXT NOT NULL DEFAULT '',
    dismissed       BOOLEAN NOT NULL DEFAULT FALSE,
    clicked         BOOLEAN NOT NULL DEFAULT FALSE,
    recommended_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_paper_recommendations_paper_id UNIQUE (paper_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_recommendations_score ON paper_recommendations(score DESC);
