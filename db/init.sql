-- =============================================================================
-- JARVIS RD Assistant - PostgreSQL Schema
-- =============================================================================
-- This file is mounted into the postgres container at
-- /docker-entrypoint-initdb.d/01_init.sql and runs on first database creation.
--
-- Conventions:
--   - All tables: snake_case, plural
--   - All timestamps: TIMESTAMPTZ (timezone-aware)
--   - All tables include created_at DEFAULT NOW()
--   - JSONB for flexible/evolving data structures
--   - Foreign keys with ON DELETE CASCADE where parent owns children
--   - This schema reflects the post-migration steady state for fresh installs.
--     Versioned migrations remain the source of truth for upgrades.
-- =============================================================================

-- =============================================================================
-- SHARED / CONFIGURATION
-- =============================================================================

CREATE TABLE user_config (
    id              SERIAL PRIMARY KEY,
    key             VARCHAR(255) UNIQUE NOT NULL,
    value           JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE user_config IS 'Key-value store for user preferences. Single-user system.';

-- Seed default configuration
INSERT INTO user_config (key, value) VALUES
    ('llm.smart_model', '"smart"'),
    ('llm.fast_model', '"fast"'),
    ('llm.embed_model', '"embed"'),
    ('notifications.timezone', '"Europe/Berlin"'),
    ('notifications.morning_briefing', '{"enabled": true, "cron": "30 8 * * *"}'),
    ('notifications.paper_digest', '{"enabled": true, "cron": "0 9 * * *"}'),
    ('notifications.review_reminder', '{"enabled": true, "cron": "0 14 * * *"}'),
    ('fsrs.desired_retention', '0.9'),
    ('fsrs.learning_steps', '[1, 10]'),
    ('paper.max_daily', '20'),
    ('paper.auto_generate_cards', 'true')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE llm_usage_log (
    id                  SERIAL PRIMARY KEY,
    provider            VARCHAR(100),
    workflow            VARCHAR(100),
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    cost_usd            DECIMAL(10, 6),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE llm_usage_log IS 'Tracks LLM token usage and costs per workflow for analytics.';

-- =============================================================================
-- MODULE 1: RESEARCH PULSE
-- =============================================================================

CREATE TABLE paper_sources (
    id              SERIAL PRIMARY KEY,
    source_type     VARCHAR(50) NOT NULL UNIQUE,
    enabled         BOOLEAN DEFAULT TRUE,
    priority        INTEGER NOT NULL DEFAULT 1,
    config          JSONB DEFAULT '{}',
    display_order   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE paper_sources IS 'Pluggable paper source registry. Each row is a configured source.';

-- Seed default sources
INSERT INTO paper_sources (source_type, enabled, config) VALUES
    ('arxiv', TRUE, '{}'),
    ('semantic_scholar', FALSE, '{"key_env": "SEMANTIC_SCHOLAR_API_KEY", "requires_key": false}'),
    ('local', FALSE, '{}')  -- TODO: Enable when local PDF ingestion is implemented
ON CONFLICT (source_type) DO NOTHING;

CREATE TABLE topics (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    query_terms     TEXT[] NOT NULL,
    category        VARCHAR(100),
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE topics IS 'User-defined research topics with search query terms.';

CREATE TABLE papers (
    id              SERIAL PRIMARY KEY,
    external_id     VARCHAR(255) UNIQUE NOT NULL,
    source_type     VARCHAR(50) NOT NULL,
    title           TEXT NOT NULL,
    authors         TEXT[] NOT NULL,
    abstract        TEXT,
    published_date  DATE,
    url             TEXT NOT NULL,
    pdf_url         TEXT,
    pdf_local_path  TEXT,
    pdf_downloaded  BOOLEAN DEFAULT FALSE,
    citation_count  INTEGER DEFAULT 0,
    priority_score  FLOAT,
    metadata        JSONB DEFAULT '{}',
    is_read         BOOLEAN DEFAULT FALSE,
    discovered_at   TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    citations_fetched_at TIMESTAMPTZ,
    search_vector   tsvector
);

COMMENT ON TABLE papers IS 'All ingested papers. Metadata comes from source APIs, never from LLMs.';

-- Full-text search support (from migration 002)
CREATE OR REPLACE FUNCTION papers_search_vector_update() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    NEW.search_vector :=
        to_tsvector('english', coalesce(NEW.title, '')) ||
        to_tsvector('english', coalesce(NEW.abstract, '')) ||
        to_tsvector('english', coalesce(array_to_string(NEW.authors, ' '), ''));
    RETURN NEW;
END;
$$;

CREATE TRIGGER papers_search_vector_trigger
    BEFORE INSERT OR UPDATE OF title, abstract, authors ON papers
    FOR EACH ROW EXECUTE FUNCTION papers_search_vector_update();

CREATE INDEX IF NOT EXISTS idx_papers_search_vector
    ON papers USING GIN(search_vector);

CREATE TABLE paper_topics (
    paper_id        INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    topic_id        INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    relevance_score FLOAT,
    PRIMARY KEY (paper_id, topic_id)
);

COMMENT ON TABLE paper_topics IS 'Many-to-many link between papers and topics with LLM-scored relevance.';

CREATE TABLE paper_chunks (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    page_number     INTEGER,
    start_char      INTEGER,
    end_char        INTEGER,
    embedding_id    VARCHAR(255),
    embedding_model VARCHAR(100),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(paper_id, chunk_index)
);

COMMENT ON TABLE paper_chunks IS 'PDF text split into chunks for RAG. Each chunk maps to a Qdrant vector.';

CREATE TABLE paper_summaries (
    id                  SERIAL PRIMARY KEY,
    paper_id            INTEGER REFERENCES papers(id) ON DELETE CASCADE UNIQUE,
    summary_brief       TEXT NOT NULL,
    summary_detailed    TEXT NOT NULL,
    tldr                TEXT,
    key_findings        JSONB NOT NULL DEFAULT '[]',
    methodology         TEXT,
    limitations         TEXT,
    relevance_notes     TEXT,
    confidence          VARCHAR(10) DEFAULT 'HIGH' CHECK (confidence IN ('HIGH', 'MEDIUM', 'LOW')),
    cross_references    JSONB DEFAULT '[]',
    llm_model           VARCHAR(100),
    llm_prompt          TEXT,
    llm_raw_response    TEXT,
    summary_verified    BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE paper_summaries IS 'LLM-generated summaries with verified citations. See key_findings JSONB.';
COMMENT ON COLUMN paper_summaries.summary_verified IS
    'TRUE only when confidence=HIGH. Summary text is LLM prose, not independently verified against source.';
COMMENT ON COLUMN paper_summaries.key_findings IS
    'Array of {finding, quote, page_number, chunk_id}. Quotes verified against source.';
COMMENT ON COLUMN paper_summaries.cross_references IS
    'Array of {related_paper_id, relationship, explanation, related_quote}.';
COMMENT ON COLUMN paper_summaries.confidence IS 'HIGH, MEDIUM, or LOW based on quote verification pass rate.';
COMMENT ON COLUMN paper_summaries.llm_prompt IS 'The exact prompt sent to the LLM (audit trail).';
COMMENT ON COLUMN paper_summaries.llm_raw_response IS 'The raw LLM response before parsing (audit trail).';

CREATE TABLE paper_user_state (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER REFERENCES papers(id) ON DELETE CASCADE UNIQUE,
    status          VARCHAR(20) DEFAULT 'new' CHECK (status IN ('new', 'reading', 'read', 'archived', 'starred')),
    user_notes      TEXT,
    rating          SMALLINT CHECK (rating BETWEEN 1 AND 5),
    flagged         BOOLEAN DEFAULT FALSE,
    notified_at     TIMESTAMPTZ,
    read_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE paper_user_state IS 'Per-paper user state: reading status, notes, rating, flag.';
COMMENT ON COLUMN paper_user_state.status IS 'One of: new, reading, read, archived, starred.';
COMMENT ON COLUMN paper_user_state.flagged IS 'User flagged this summary as potentially inaccurate.';

-- Paper recommendations (from migration 017)
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

CREATE TABLE paper_notes (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_note       TEXT NOT NULL,
    highlight_text  TEXT,
    page_number     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE paper_notes IS 'User annotations on papers, optionally linked to a page or highlighted text.';

CREATE INDEX idx_paper_notes_paper ON paper_notes(paper_id);

-- =============================================================================
-- CITATION GRAPH
-- =============================================================================

CREATE TABLE paper_citations (
    id               SERIAL PRIMARY KEY,
    source_paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    cited_paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    citation_context TEXT,
    is_influential   BOOLEAN,
    intent           TEXT[],
    fetched_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_paper_id, cited_paper_id)
);

COMMENT ON TABLE paper_citations IS 'Citation relationships between papers fetched from Semantic Scholar.';

CREATE INDEX IF NOT EXISTS idx_citations_source ON paper_citations(source_paper_id);
CREATE INDEX IF NOT EXISTS idx_citations_cited ON paper_citations(cited_paper_id);

-- =============================================================================
-- MODULE 2: LEARNING ENGINE
-- =============================================================================

CREATE TABLE decks (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    topic_id        INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE decks IS 'Flashcard decks, optionally linked to a research topic.';

CREATE TABLE cards (
    id              SERIAL PRIMARY KEY,
    deck_id         INTEGER REFERENCES decks(id) ON DELETE CASCADE,
    paper_id        INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    card_type       VARCHAR(20) NOT NULL CHECK (card_type IN ('concept', 'quote', 'method', 'comparison')),
    front           TEXT NOT NULL,
    back            TEXT NOT NULL,
    evidence        JSONB NOT NULL DEFAULT '{}',
    fsrs_state      JSONB NOT NULL DEFAULT '{}',
    due_at          TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE cards IS 'Spaced repetition flashcards with FSRS scheduling state.';
COMMENT ON COLUMN cards.card_type IS 'One of: concept, quote, method, comparison.';
COMMENT ON COLUMN cards.evidence IS '{quote, page_number, chunk_id, pdf_snapshot_path}.';
COMMENT ON COLUMN cards.fsrs_state IS 'Serialized py-fsrs Card: stability, difficulty, reps, lapses, state, due.';
COMMENT ON COLUMN cards.due_at IS 'Denormalized from fsrs_state.due for efficient indexed queries.';

CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(due_at) WHERE due_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck_id);
CREATE INDEX IF NOT EXISTS idx_cards_paper ON cards(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_chunks_paper ON paper_chunks(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_topics_topic ON paper_topics(topic_id);
CREATE INDEX IF NOT EXISTS idx_papers_created ON papers(created_at);
CREATE INDEX IF NOT EXISTS idx_papers_unread ON papers(is_read) WHERE is_read = FALSE;
CREATE INDEX IF NOT EXISTS idx_papers_priority ON papers(priority_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage_log(created_at);

CREATE TABLE review_logs (
    id                  SERIAL PRIMARY KEY,
    card_id             INTEGER REFERENCES cards(id) ON DELETE CASCADE,
    rating              SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 4),
    review_duration_ms  INTEGER,
    reviewed_at         TIMESTAMPTZ DEFAULT NOW(),
    fsrs_log            JSONB NOT NULL DEFAULT '{}'
);

COMMENT ON TABLE review_logs IS 'History of flashcard reviews. Rating: 1=Again, 2=Hard, 3=Good, 4=Easy.';

-- =============================================================================
-- MODULE 3: PROJECT MANAGER
-- =============================================================================

CREATE TABLE projects (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    status          VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'paused', 'completed', 'archived')),
    deadline        TIMESTAMPTZ,
    color           VARCHAR(7) CHECK (color IS NULL OR color ~ '^#[0-9A-Fa-f]{6}$'),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE projects IS 'Research projects with deadlines.';
COMMENT ON COLUMN projects.status IS 'One of: active, paused, completed, archived.';

CREATE TABLE tasks (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    parent_task_id  INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title           VARCHAR(500) NOT NULL,
    description     TEXT,
    status          VARCHAR(20) DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'blocked', 'done')),
    priority        SMALLINT DEFAULT 3 CHECK (priority BETWEEN 1 AND 4),
    deadline        TIMESTAMPTZ,
    estimated_hours FLOAT,
    actual_hours    FLOAT,
    sort_order      INTEGER DEFAULT 0,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE tasks IS 'Tasks within projects. Supports subtasks via parent_task_id.';
COMMENT ON COLUMN tasks.status IS 'One of: todo, in_progress, blocked, done.';
COMMENT ON COLUMN tasks.priority IS '1=critical, 2=high, 3=medium, 4=low.';

CREATE TABLE task_paper_links (
    task_id         INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    paper_id        INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    note            TEXT,
    PRIMARY KEY (task_id, paper_id)
);

COMMENT ON TABLE task_paper_links IS 'Links papers to tasks for project-scoped reading lists.';

CREATE INDEX IF NOT EXISTS idx_task_paper_links_paper ON task_paper_links(paper_id);
CREATE INDEX IF NOT EXISTS idx_task_paper_links_task ON task_paper_links(task_id);

CREATE TABLE milestones (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    deadline        TIMESTAMPTZ NOT NULL,
    description     TEXT,
    completed       BOOLEAN DEFAULT FALSE,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE milestones IS 'Project milestones with deadlines for Telegram reminder nudges.';

CREATE TABLE IF NOT EXISTS project_papers (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id    INTEGER NOT NULL REFERENCES papers(id)   ON DELETE CASCADE,
    notes       TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, paper_id)
);

COMMENT ON TABLE project_papers IS 'Many-to-many link between projects and papers with optional notes.';

CREATE INDEX IF NOT EXISTS idx_project_papers_project ON project_papers(project_id);
CREATE INDEX IF NOT EXISTS idx_project_papers_paper   ON project_papers(paper_id);

CREATE TABLE daily_log (
    id              SERIAL PRIMARY KEY,
    log_date        DATE NOT NULL UNIQUE,
    tasks_completed INTEGER DEFAULT 0,
    cards_reviewed  INTEGER DEFAULT 0,
    papers_read     INTEGER DEFAULT 0,
    focus_hours     FLOAT DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE daily_log IS 'Daily activity summary for analytics and streaks.';

CREATE TABLE scheduled_nudges (
    id              SERIAL PRIMARY KEY,
    nudge_type      VARCHAR(50) NOT NULL UNIQUE CHECK (nudge_type IN ('deadline_warning', 'daily_summary', 'review_reminder', 'paper_digest', 'research_pulse', 'author_alert')),
    cron_expression VARCHAR(100) NOT NULL,
    enabled         BOOLEAN DEFAULT TRUE,
    config          JSONB DEFAULT '{}',
    last_fired_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE scheduled_nudges IS 'Configurable notification schedules for Telegram nudges.';
COMMENT ON COLUMN scheduled_nudges.nudge_type IS
    'One of: deadline_warning, daily_summary, review_reminder, paper_digest, research_pulse, author_alert.';

-- Seed default nudges
INSERT INTO scheduled_nudges (nudge_type, cron_expression, enabled) VALUES
    ('daily_summary', '30 8 * * *', TRUE),
    ('paper_digest', '0 9 * * 1', TRUE),
    ('review_reminder', '0 14 * * *', TRUE),
    ('deadline_warning', '0 12 * * *', TRUE),
    ('research_pulse', '0 9 * * *', TRUE),
    ('author_alert', '0 10 * * *', TRUE)
ON CONFLICT (nudge_type) DO NOTHING;

-- =============================================================================
-- AUTHOR TRACKING
-- =============================================================================

CREATE TABLE tracked_authors (
    id              SERIAL PRIMARY KEY,
    author_name     TEXT NOT NULL,
    s2_author_id    VARCHAR(50),
    source          VARCHAR(20) NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'auto_starred', 'auto_rated')),
    enabled         BOOLEAN DEFAULT TRUE,
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (author_name, s2_author_id)
);

COMMENT ON TABLE tracked_authors IS 'Authors to track for new-publication alerts.';

CREATE INDEX IF NOT EXISTS idx_tracked_authors_enabled
    ON tracked_authors(enabled) WHERE enabled = TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_authors_name_no_s2
    ON tracked_authors (author_name) WHERE s2_author_id IS NULL;

CREATE TABLE author_alert_log (
    id                SERIAL PRIMARY KEY,
    tracked_author_id INTEGER REFERENCES tracked_authors(id) ON DELETE CASCADE,
    paper_id          INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    notified_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tracked_author_id, paper_id)
);

COMMENT ON TABLE author_alert_log IS 'Deduplication log for author alert notifications.';

-- =============================================================================
-- STRUCTURED DATA EXTRACTION
-- =============================================================================

CREATE TABLE extraction_templates (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(255) NOT NULL UNIQUE,
    description   TEXT,
    fields        JSONB NOT NULL DEFAULT '[]',
    is_default    BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE extraction_templates IS 'User-defined templates for extracting structured fields from papers.';

INSERT INTO extraction_templates (name, description, fields, is_default) VALUES
    ('Standard Research Paper', 'Default template for empirical research papers',
     '[{"name":"methodology","label":"Methodology","description":"Research methodology used","type":"text"},
       {"name":"sample_size","label":"Sample Size","description":"Number of participants or samples","type":"number"},
       {"name":"main_finding","label":"Main Finding","description":"Primary result or conclusion","type":"text"},
       {"name":"limitations","label":"Limitations","description":"Acknowledged limitations","type":"text"},
       {"name":"future_work","label":"Future Work","description":"Suggested future directions","type":"text"}]',
     TRUE)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE paper_extractions (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    template_id     INTEGER NOT NULL REFERENCES extraction_templates(id) ON DELETE CASCADE,
    extractions     JSONB NOT NULL DEFAULT '{}',
    extraction_model VARCHAR(100),
    extraction_raw   TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (paper_id, template_id)
);

COMMENT ON TABLE paper_extractions IS 'LLM-extracted structured data from papers using templates.';

CREATE INDEX IF NOT EXISTS idx_paper_extractions_paper ON paper_extractions(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_extractions_template ON paper_extractions(template_id);

-- =============================================================================
-- KNOWLEDGE GRAPH
-- =============================================================================

CREATE TABLE entities (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    canonical_name  TEXT NOT NULL,
    entity_type     VARCHAR(50) NOT NULL
        CHECK (entity_type IN ('method', 'dataset', 'metric', 'author', 'institution', 'concept')),
    description     TEXT,
    metadata        JSONB DEFAULT '{}',
    embedding_id    VARCHAR(255),
    paper_count     INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(canonical_name, entity_type)
);

COMMENT ON TABLE entities IS 'Knowledge graph entities extracted from papers via LLM.';

CREATE TABLE entity_relationships (
    id                SERIAL PRIMARY KEY,
    source_entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relationship_type VARCHAR(100) NOT NULL,
    paper_id          INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    evidence_quote    TEXT,
    confidence        FLOAT DEFAULT 1.0,
    metadata          JSONB DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_entity_id, target_entity_id, relationship_type, paper_id)
);

COMMENT ON TABLE entity_relationships IS 'Relationships between knowledge graph entities with evidence.';

CREATE TABLE paper_entities (
    paper_id        INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    entity_id       INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    mention_count   INTEGER DEFAULT 1,
    first_chunk_id  INTEGER REFERENCES paper_chunks(id) ON DELETE SET NULL,
    PRIMARY KEY (paper_id, entity_id)
);

COMMENT ON TABLE paper_entities IS 'Many-to-many link between papers and extracted entities.';

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entity_rels_source ON entity_relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_rels_target ON entity_relationships(target_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_rels_paper ON entity_relationships(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_entities_entity ON paper_entities(entity_id);

CREATE INDEX IF NOT EXISTS idx_review_logs_card ON review_logs(card_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id);
CREATE INDEX IF NOT EXISTS idx_papers_source_type ON papers(source_type);
CREATE INDEX IF NOT EXISTS idx_paper_user_state_status ON paper_user_state(status);

-- =============================================================================
-- MODULE: DISCOVERY & PULSE (migration 018)
-- =============================================================================

-- Extend topics with optional description for LLM context
ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS description TEXT;
COMMENT ON COLUMN topics.description IS
    'Optional free-text context used by the Pulse scoring LLM. Null = fall back to name + query_terms.';

-- Pulse decks — one row per day per user, holds the curated card set
CREATE TABLE IF NOT EXISTS pulse_decks (
    id              SERIAL PRIMARY KEY,
    deck_date       DATE NOT NULL UNIQUE,  -- single-user system
    card_count      INTEGER NOT NULL DEFAULT 0,
    generated_at    TIMESTAMPTZ DEFAULT NOW(),
    stats           JSONB DEFAULT '{}'::jsonb,  -- candidate count, LLM calls, duration, etc.
    degraded_reason TEXT
);

-- Pulse cards — the papers in each deck with score metadata
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

-- Pulse ratings — feedback loop, collected from Phase 1 onward
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

-- PDF resolution cache — dedup resolver calls, supports ingestion fallback
CREATE TABLE IF NOT EXISTS pdf_resolutions (
    id              SERIAL PRIMARY KEY,
    doi             VARCHAR(255),
    arxiv_id        VARCHAR(50),
    resolved_url    TEXT,         -- null = resolution failed
    resolver_name   VARCHAR(32),  -- 'arxiv' / 'unpaywall' / 'core' / 'failed'
    resolved_at     TIMESTAMPTZ DEFAULT NOW(),
    -- NULLS NOT DISTINCT: two rows with the same (doi, arxiv_id) pair are
    -- considered duplicates even when one or both columns are NULL.
    -- Requires PostgreSQL 15+.
    UNIQUE NULLS NOT DISTINCT (doi, arxiv_id)
);
CREATE INDEX IF NOT EXISTS idx_pdf_resolutions_doi
    ON pdf_resolutions(doi) WHERE doi IS NOT NULL;

-- Async job queue — generic status machine for long-running background tasks
CREATE TABLE IF NOT EXISTS jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  progress REAL NOT NULL DEFAULT 0.0,
  progress_message TEXT,
  result JSONB,
  error JSONB,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  user_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_kind ON jobs(status, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_created_desc ON jobs(created_at DESC);

-- Register new source types for Discovery & Pulse
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

-- Seed Pulse scoring weights in user_config
INSERT INTO user_config (key, value) VALUES
    ('pulse.enabled', 'false'::jsonb),
    ('pulse.cron', '"0 4 * * *"'::jsonb),
    ('pulse.deck_size', '10'::jsonb),
    ('pulse.stage2_top_k', '50'::jsonb),
    ('pulse.weights',
     '{"embedding": 0.2, "topic": 0.2, "llm_relevance": 0.3, "llm_novelty": 0.1, "author_bonus": 0.15, "recency": 0.05}'::jsonb)
ON CONFLICT (key) DO NOTHING;
