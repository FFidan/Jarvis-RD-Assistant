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
-- SHARED HELPERS
-- =============================================================================
-- Defined here (rather than alongside its consuming triggers near the bottom of
-- the file) because triggers from migrations 046+ reference set_updated_at()
-- before the section that originally introduced it (migration 042). The
-- CREATE OR REPLACE block further down is harmless — it idempotently re-applies
-- the same body and then attaches the legacy migration-042 triggers.

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END $$ LANGUAGE plpgsql;

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
    ('user.timezone', '"UTC"'),
    ('fsrs.desired_retention', '0.9'),
    ('fsrs.learning_steps', '[1, 10]')
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
    discovered_at   TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    citations_fetched_at TIMESTAMPTZ,
    search_vector   tsvector
);

COMMENT ON TABLE papers IS 'All ingested papers. Metadata comes from source APIs, never from LLMs.';

-- Wave 6: per-user ownership column (migration 042). NULL = system-owned / single-user mode.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_papers_user ON papers(user_id) WHERE user_id IS NOT NULL;

-- Phase A (migration 048): discovery_origin tracks how each paper first entered.
-- Used by frontend to conditionally render feedback (👍/👎) buttons only on
-- machine-recommended papers. Immutable after insert.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS discovery_origin TEXT NOT NULL DEFAULT 'user_initiated'
    CHECK (discovery_origin IN ('user_initiated', 'pulse', 'recommender', 'citation_batch'));
CREATE INDEX IF NOT EXISTS idx_papers_discovery_origin ON papers(discovery_origin);
COMMENT ON COLUMN papers.discovery_origin IS
    'How the paper first entered the system. Immutable. Values: user_initiated (manual search/upload/Zotero/citation graph), pulse (overnight discovery), recommender (paper_recommendations), citation_batch (citation graph batch save).';

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

-- Wave 6: per-user ownership (migration 042).
ALTER TABLE paper_chunks ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_paper_chunks_user ON paper_chunks(user_id) WHERE user_id IS NOT NULL;

CREATE TABLE paper_summaries (
    id                  SERIAL PRIMARY KEY,
    paper_id            INTEGER REFERENCES papers(id) ON DELETE CASCADE,
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

-- Wave 6: per-user ownership (migration 042).
ALTER TABLE paper_summaries ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_paper_summaries_user ON paper_summaries(user_id) WHERE user_id IS NOT NULL;
-- Migration 043: (paper_id, user_id) unique replaces single-paper UNIQUE.
ALTER TABLE paper_summaries
    ADD CONSTRAINT paper_summaries_paper_id_user_id_key
    UNIQUE NULLS NOT DISTINCT (paper_id, user_id);

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

-- Wave 6: per-user ownership (migration 042).
ALTER TABLE paper_contradictions ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_paper_contradictions_user ON paper_contradictions(user_id) WHERE user_id IS NOT NULL;

-- Phase A (migration 047): paper_user_state collapsed to single-state ENUM.
-- The 5 booleans + status enum from migration 046 (saved/dismissed/archived/status/preference)
-- are replaced by one `state` ENUM ('inbox','to_read','reading','done','trash') plus
-- a `state_before_trash` ENUM column for Restore. See spec §3.1.
CREATE TABLE paper_user_state (
    id                  SERIAL PRIMARY KEY,
    paper_id            INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    state               TEXT NOT NULL DEFAULT 'inbox'
                            CHECK (state IN ('inbox', 'to_read', 'reading', 'done', 'trash')),
    state_before_trash  TEXT
                            CHECK (state_before_trash IS NULL
                                   OR state_before_trash IN ('inbox', 'to_read', 'reading', 'done')),
    starred             BOOLEAN NOT NULL DEFAULT FALSE,
    user_notes          TEXT,
    rating              SMALLINT CHECK (rating BETWEEN 1 AND 5),
    flagged             BOOLEAN DEFAULT FALSE,
    notified_at         TIMESTAMPTZ,
    read_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE paper_user_state IS 'Per-paper user state: lifecycle position (state), star (curation flag), reading metadata. Recommendation feedback lives in a separate table (recommendation_feedback).';

-- Wave 6: per-user ownership (migration 042).
ALTER TABLE paper_user_state ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_paper_user_state_user ON paper_user_state(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_user_state_state ON paper_user_state(state);
-- Migration 043: (paper_id, user_id) unique replaces single-paper UNIQUE.
ALTER TABLE paper_user_state
    ADD CONSTRAINT paper_user_state_paper_id_user_id_key
    UNIQUE NULLS NOT DISTINCT (paper_id, user_id);
COMMENT ON COLUMN paper_user_state.state IS 'Lifecycle position: inbox (untriaged), to_read (saved), reading (engaging), done (finished), trash (rejected). Replaces 5 booleans + status enum from migration 046.';
COMMENT ON COLUMN paper_user_state.state_before_trash IS 'For trash rows: the state to restore to. NULL for non-trash rows.';
COMMENT ON COLUMN paper_user_state.starred IS 'Per-user favourite flag, orthogonal to state. Triggers zotero.push when project-linked.';
COMMENT ON COLUMN paper_user_state.flagged IS 'User flagged this summary as potentially inaccurate.';
-- Migration 046: updated_at trigger (set_updated_at function defined in migration 042).
DROP TRIGGER IF EXISTS set_updated_at_paper_user_state ON paper_user_state;
CREATE TRIGGER set_updated_at_paper_user_state
    BEFORE UPDATE ON paper_user_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

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
    source          TEXT NOT NULL DEFAULT 'user'
        CHECK (source IN ('user', 'zotero')),
    zotero_annotation_key TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'verified', 'failed')),
    verified_quote TEXT,
    verified_page_number INTEGER,
    promoted_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE paper_notes IS 'User annotations on papers, optionally linked to a page or highlighted text.';

-- Wave 6: per-user ownership (migration 042).
ALTER TABLE paper_notes ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_paper_notes_user ON paper_notes(user_id) WHERE user_id IS NOT NULL;

CREATE INDEX idx_paper_notes_paper ON paper_notes(paper_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_notes_zotero_annotation
    ON paper_notes(paper_id, zotero_annotation_key)
    WHERE zotero_annotation_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_notes_paper_source
    ON paper_notes(paper_id, source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_notes_search
    ON paper_notes
    USING GIN (to_tsvector('english', coalesce(user_note, '') || ' ' || coalesce(highlight_text, '')));

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
    user_id         INTEGER,
    log_date        DATE NOT NULL,
    tasks_completed INTEGER DEFAULT 0,
    cards_reviewed  INTEGER DEFAULT 0,
    papers_read     INTEGER DEFAULT 0,
    focus_hours     FLOAT DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT daily_log_user_id_log_date_key UNIQUE NULLS NOT DISTINCT (user_id, log_date)
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
    -- NULLS NOT DISTINCT: two NULL s2_author_id values are treated as equal
    -- (matches migration 021 semantics; requires PostgreSQL 15+).
    CONSTRAINT tracked_authors_name_s2_unique
        UNIQUE NULLS NOT DISTINCT (author_name, s2_author_id)
);

COMMENT ON TABLE tracked_authors IS 'Authors to track for new-publication alerts.';

CREATE INDEX IF NOT EXISTS idx_tracked_authors_enabled
    ON tracked_authors(enabled) WHERE enabled = TRUE;

-- Companion partial index for IS NULL author-name lookups (migration 029).
-- The UNIQUE NULLS NOT DISTINCT constraint covers uniqueness; this index
-- speeds up the IS NULL half of author-alert / author-tracking queries.
CREATE INDEX IF NOT EXISTS idx_tracked_authors_name_null_s2
    ON tracked_authors (author_name)
    WHERE s2_author_id IS NULL;

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

-- Wave 6: per-user ownership (migration 042).
ALTER TABLE paper_extractions ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_paper_extractions_user ON paper_extractions(user_id) WHERE user_id IS NOT NULL;

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
    page_number       INTEGER,
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
-- (Phase A migration 047 collapsed paper_user_state.status into the state ENUM;
-- the replacement idx_paper_user_state_state index is created above alongside
-- the table definition at line ~295.)

-- Sprint 7 B5: functional indexes for search-preview candidate-key matching
-- (mirrors db/migrations/045_papers_search_preview_indexes.sql).
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
    deck_date       DATE NOT NULL,  -- uniqueness per (deck_date, user_id) — see constraint below
    card_count      INTEGER NOT NULL DEFAULT 0,
    generated_at    TIMESTAMPTZ DEFAULT NOW(),
    stats           JSONB DEFAULT '{}'::jsonb,  -- candidate count, LLM calls, duration, etc.
    degraded_reason TEXT,
    -- Migration 043: nullable user_id for future per-user deck segregation.
    user_id         INTEGER NULL
);
-- (deck_date, user_id) UNIQUE NULLS NOT DISTINCT: single-tenant rows (user_id=NULL)
-- deduplicate correctly while multi-user rows are isolated per user.
-- Requires PostgreSQL 15+ (project uses PG 16).
ALTER TABLE pulse_decks
    ADD CONSTRAINT pulse_decks_deck_date_user_id_key
    UNIQUE NULLS NOT DISTINCT (deck_date, user_id);
CREATE INDEX IF NOT EXISTS idx_pulse_decks_user
    ON pulse_decks(user_id) WHERE user_id IS NOT NULL;

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
    -- Migration 042/043: nullable user_id for per-user ownership.
    user_id         INTEGER NULL,
    UNIQUE (deck_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_pulse_cards_deck_rank
    ON pulse_cards(deck_id, rank);
CREATE INDEX IF NOT EXISTS idx_pulse_cards_user ON pulse_cards(user_id) WHERE user_id IS NOT NULL;

-- Phase A (migration 049): recommendation_feedback replaces pulse_ratings.
-- Single source of truth for recommendation-quality user signals (👍/👎/🗑+👎).
-- Decoupled from paper_user_state.state lifecycle on purpose: 👎 does not trash
-- a paper, Trash does not write 👎. Pulse stage-2 reranker (L1), pulse stage-1
-- cosine penalty (L2), and recommender hard exclusion + topic dampening (L3)
-- all read from this single table. See spec §3.3 + §7.
CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id              BIGSERIAL PRIMARY KEY,
    paper_id        BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id         INTEGER,                                      -- NULL = single-tenant
    signal          TEXT NOT NULL CHECK (signal IN ('positive', 'negative')),
    source          TEXT NOT NULL CHECK (source IN (
        'pulse_thumbs',          -- 👍/👎 on Pulse Deck card
        'feed_thumbs',           -- 👍/👎 on Inbox/Library row (Pulse-origin only)
        'paper_detail_thumbs',   -- 👍/👎 on Paper Detail page
        'dismiss_combined'       -- 🗑+👎 combined button
    )),
    topic_id        BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT recommendation_feedback_paper_user_source_uniq
        UNIQUE NULLS NOT DISTINCT (paper_id, user_id, source)
);

CREATE INDEX IF NOT EXISTS recommendation_feedback_paper_idx
    ON recommendation_feedback (paper_id);
CREATE INDEX IF NOT EXISTS recommendation_feedback_signal_recent_idx
    ON recommendation_feedback (signal, created_at DESC);
CREATE INDEX IF NOT EXISTS recommendation_feedback_topic_idx
    ON recommendation_feedback (topic_id) WHERE topic_id IS NOT NULL;

COMMENT ON TABLE recommendation_feedback IS
    'Single source of truth for recommendation-quality user signals. Replaces pulse_ratings (dropped in migration 049).';

CREATE TABLE IF NOT EXISTS pulse_models (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER,
    model_version   TEXT NOT NULL DEFAULT 'v1',
    model_blob      BYTEA NOT NULL,
    feature_names   JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    trained_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pulse_models_one_active_per_user
    ON pulse_models (coalesce(user_id, 0))
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_pulse_models_trained_at
    ON pulse_models(trained_at DESC);

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
    ('pulse.stage2_top_k', '40'::jsonb),
    ('pulse.weights',
     '{"embedding": 0.2, "topic": 0.2, "llm_relevance": 0.3, "llm_novelty": 0.1, "author_bonus": 0.15, "recency": 0.05, "citation_pagerank": 0.0, "citation_count": 0.0, "citation_adamic_adar": 0.0, "classifier": 0.0}'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- Telegram pairing — short-lived codes used by the setup wizard to link a
-- Telegram chat to the JARVIS owner (migration 020).
CREATE TABLE IF NOT EXISTS telegram_pairing (
    code        TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS telegram_pairing_expires_idx
    ON telegram_pairing (expires_at);

-- Seed setup / Telegram config keys in user_config (migration 020).
INSERT INTO user_config (key, value) VALUES
    ('telegram.owner_chat_id', 'null'::jsonb),
    ('setup.completed',        'false'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- Audit log for security events + destructive mutations (migration 030).
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action, timestamp DESC);

-- =============================================================================
-- MODULE: JOURNAL ENTRIES (migration 051)
-- End-of-day reflection prompts. user_id follows INTEGER NULL convention.
-- =============================================================================

CREATE TABLE IF NOT EXISTS journal_entries (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER,
    date        DATE NOT NULL DEFAULT CURRENT_DATE,
    prompts     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-user-per-date uniqueness (NULLS NOT DISTINCT so single-tenant NULL rows
-- still deduplicate correctly).
DO $$ BEGIN
    ALTER TABLE journal_entries
        ADD CONSTRAINT journal_entries_user_id_date_key
        UNIQUE NULLS NOT DISTINCT (user_id, date);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- =============================================================================
-- MODULE: JOB PROGRESS (migration 054 + 058)
-- Persistent progress + terminal outcome storage for procrastinate-backed jobs.
-- =============================================================================

CREATE TABLE IF NOT EXISTS job_progress (
    jarvis_job_id TEXT PRIMARY KEY,
    progress      REAL NOT NULL DEFAULT 0,
    message       TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Migration 058: terminal result/error payloads.
    result        JSONB,
    error         JSONB
);

CREATE INDEX IF NOT EXISTS ix_job_progress_updated_at ON job_progress(updated_at);

-- =============================================================================
-- MODULE: DAILY INTENT (migrations 059 + 060)
-- Today's Intent persistence with INTEGER NULL user_id convention (post-060).
-- =============================================================================

CREATE TABLE IF NOT EXISTS daily_intent (
    user_id     INTEGER NULL,
    intent_date DATE NOT NULL,
    intent_text TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS daily_intent_user_date_uniq
    ON daily_intent (user_id, intent_date) NULLS NOT DISTINCT;

-- =============================================================================
-- DB-004: shared updated_at trigger (migration 042)
-- Keeps updated_at current on every UPDATE for tables that have the column.
-- =============================================================================

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_config_updated_at ON user_config;
CREATE TRIGGER trg_user_config_updated_at
    BEFORE UPDATE ON user_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_paper_contradictions_updated_at ON paper_contradictions;
CREATE TRIGGER trg_paper_contradictions_updated_at
    BEFORE UPDATE ON paper_contradictions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_cards_updated_at ON cards;
CREATE TRIGGER trg_cards_updated_at
    BEFORE UPDATE ON cards
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
CREATE TRIGGER trg_projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_tasks_updated_at ON tasks;
CREATE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_extraction_templates_updated_at ON extraction_templates;
CREATE TRIGGER trg_extraction_templates_updated_at
    BEFORE UPDATE ON extraction_templates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- SCHEMA-MIGRATIONS BOOTSTRAP
-- =============================================================================
-- This file mirrors most of the post-migration steady state for fresh installs.
-- Pre-populate schema_migrations only for migrations already baked into this
-- snapshot. Later additive/corrective migrations are intentionally left absent
-- so the runtime runner applies them on first boot. Do not use generate_series:
-- it can falsely mark migrations as applied when init.sql does not embody them.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO schema_migrations (version) VALUES
    (1), (2), (3), (4), (5), (6), (7), (8),
    (9), (10), (11), (12), (13), (14), (15), (16),
    (17), (18), (19), (20), (21), (22), (23), (24),
    (25), (26), (27), (28), (29), (30), (31), (32),
    -- 33 is intentionally absent: it is false-applied and repaired at runtime.
    (34), (35), (36), (37), (38), (39), (40), (41),
    (42), (43), (44), (45), (46), (47), (48),
    -- 49-51, 54-62 are baked into this snapshot.
    -- 52 (procrastinate schema) and 53 (drop legacy jobs) are NOT in init.sql;
    -- the runtime runner will apply them on first boot.
    -- 62 (daily_log user_id + UNIQUE NULLS NOT DISTINCT guard) is baked into this snapshot.
    (49), (50), (51),
    (54), (55), (56), (57), (58), (59), (60), (61),
    (62)
ON CONFLICT (version) DO NOTHING;
