-- Migration 006: Paper notes and annotations
-- Allows users to attach text notes to papers, optionally linked to
-- a specific page or highlighted text.

CREATE TABLE IF NOT EXISTS paper_notes (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_note       TEXT NOT NULL,
    highlight_text  TEXT,
    page_number     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_notes_paper ON paper_notes(paper_id);
