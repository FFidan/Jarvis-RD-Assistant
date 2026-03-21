-- Migration 011: Structured Data Extraction
-- Adds extraction_templates and paper_extractions tables

CREATE TABLE IF NOT EXISTS extraction_templates (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(255) NOT NULL UNIQUE,
    description   TEXT,
    fields        JSONB NOT NULL DEFAULT '[]',
    is_default    BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO extraction_templates (name, description, fields, is_default) VALUES
    ('Standard Research Paper', 'Default template for empirical research papers',
     '[{"name":"methodology","label":"Methodology","description":"Research methodology used","type":"text"},
       {"name":"sample_size","label":"Sample Size","description":"Number of participants or samples","type":"number"},
       {"name":"main_finding","label":"Main Finding","description":"Primary result or conclusion","type":"text"},
       {"name":"limitations","label":"Limitations","description":"Acknowledged limitations","type":"text"},
       {"name":"future_work","label":"Future Work","description":"Suggested future directions","type":"text"}]',
     TRUE) ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS paper_extractions (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    template_id     INTEGER NOT NULL REFERENCES extraction_templates(id) ON DELETE CASCADE,
    extractions     JSONB NOT NULL DEFAULT '{}',
    extraction_model VARCHAR(100),
    extraction_raw   TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (paper_id, template_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_extractions_paper ON paper_extractions(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_extractions_template ON paper_extractions(template_id);
