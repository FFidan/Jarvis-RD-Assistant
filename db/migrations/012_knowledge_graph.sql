-- Migration 012: Knowledge Graph
-- Adds entities, entity_relationships, and paper_entities tables

CREATE TABLE IF NOT EXISTS entities (
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

CREATE TABLE IF NOT EXISTS entity_relationships (
    id                SERIAL PRIMARY KEY,
    source_entity_id  INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id  INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    relationship_type VARCHAR(100) NOT NULL,
    paper_id          INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    evidence_quote    TEXT,
    confidence        FLOAT DEFAULT 1.0,
    metadata          JSONB DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_entity_id, target_entity_id, relationship_type, paper_id)
);

CREATE TABLE IF NOT EXISTS paper_entities (
    paper_id        INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    entity_id       INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    mention_count   INTEGER DEFAULT 1,
    first_chunk_id  INTEGER REFERENCES paper_chunks(id) ON DELETE SET NULL,
    PRIMARY KEY (paper_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entity_rels_source ON entity_relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_rels_target ON entity_relationships(target_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_rels_paper ON entity_relationships(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_entities_entity ON paper_entities(entity_id);
