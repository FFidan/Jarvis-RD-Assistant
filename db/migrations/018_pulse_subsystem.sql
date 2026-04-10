-- Migration 018: Discovery & Pulse subsystem
--
-- Adds the Pulse scoring pipeline tables, PDF resolution cache,
-- new paper source registrations, and seed user_config keys.
-- Fully idempotent: every CREATE uses IF NOT EXISTS, every INSERT
-- uses ON CONFLICT DO NOTHING.

-- 1. Extend topics with optional description for LLM context
ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS description TEXT;
COMMENT ON COLUMN topics.description IS
    'Optional free-text context used by the Pulse scoring LLM. Null = fall back to name + query_terms.';

-- 2. Pulse decks — one row per day per user, holds the curated card set
CREATE TABLE IF NOT EXISTS pulse_decks (
    id              SERIAL PRIMARY KEY,
    deck_date       DATE NOT NULL UNIQUE,  -- single-user system
    card_count      INTEGER NOT NULL DEFAULT 0,
    generated_at    TIMESTAMPTZ DEFAULT NOW(),
    stats           JSONB DEFAULT '{}'::jsonb  -- candidate count, LLM calls, duration, etc.
);

-- 3. Pulse cards — the papers in each deck with score metadata
CREATE TABLE IF NOT EXISTS pulse_cards (
    id              SERIAL PRIMARY KEY,
    deck_id         INTEGER NOT NULL REFERENCES pulse_decks(id) ON DELETE CASCADE,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    rank            INTEGER NOT NULL,
    score           FLOAT NOT NULL,
    llm_relevance   INTEGER,      -- 1-10 from Stage 2
    llm_novelty     INTEGER,      -- 1-10 from Stage 2
    reasoning       TEXT,         -- one-sentence explanation from LLM
    signals         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {embedding: 0.82, topic: 0.74, author: 1, ...}
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (deck_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_pulse_cards_deck_rank
    ON pulse_cards(deck_id, rank);

-- 4. Pulse ratings — feedback loop, collected from Phase 1 onward
CREATE TABLE IF NOT EXISTS pulse_ratings (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    rating          VARCHAR(16) NOT NULL
        CHECK (rating IN ('up', 'down', 'save', 'dismiss', 'open')),
    source          VARCHAR(32) NOT NULL DEFAULT 'pulse',  -- allows future non-Pulse ratings
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pulse_ratings_paper
    ON pulse_ratings(paper_id);
CREATE INDEX IF NOT EXISTS idx_pulse_ratings_created
    ON pulse_ratings(created_at DESC);

-- 5. PDF resolution cache — dedup resolver calls, supports ingestion fallback
CREATE TABLE IF NOT EXISTS pdf_resolutions (
    id              SERIAL PRIMARY KEY,
    doi             VARCHAR(255),
    arxiv_id        VARCHAR(50),
    resolved_url    TEXT,         -- null = resolution failed
    resolver_name   VARCHAR(32),  -- 'arxiv' / 'unpaywall' / 'core' / 'failed'
    resolved_at     TIMESTAMPTZ DEFAULT NOW(),
    -- NULL=NULL distinctness is intentional: rows with neither DOI nor arXiv ID
    -- are allowed to coexist (unknown-identifier cache misses).
    UNIQUE (doi, arxiv_id)
);
CREATE INDEX IF NOT EXISTS idx_pdf_resolutions_doi
    ON pdf_resolutions(doi) WHERE doi IS NOT NULL;

-- 6. Register new source types
INSERT INTO paper_sources (source_type, enabled, config)
VALUES
    ('openalex', FALSE,
     '{"requires_key": true, "key_env": "OPENALEX_API_KEY",
       "homepage": "https://openalex.org",
       "docs": "https://developers.openalex.org/"}'::jsonb),
    ('pubmed', TRUE,
     '{"requires_key": false, "key_env": "PUBMED_API_KEY",
       "homepage": "https://pubmed.ncbi.nlm.nih.gov",
       "docs": "https://www.ncbi.nlm.nih.gov/home/develop/api/"}'::jsonb)
ON CONFLICT (source_type) DO NOTHING;

-- 7. Seed Pulse scoring weights in user_config
INSERT INTO user_config (key, value) VALUES
    ('pulse.enabled', 'false'::jsonb),
    ('pulse.cron', '"0 4 * * *"'::jsonb),
    ('pulse.deck_size', '10'::jsonb),
    ('pulse.stage2_top_k', '50'::jsonb),
    ('pulse.weights',
     '{"embedding": 0.2, "topic": 0.2, "llm_relevance": 0.3, "llm_novelty": 0.1, "author_bonus": 0.15, "recency": 0.05}'::jsonb)
ON CONFLICT (key) DO NOTHING;
