-- Migration 036: Paper note sources and Zotero annotation import metadata
BEGIN;

ALTER TABLE paper_notes
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'user'
        CHECK (source IN ('user', 'zotero')),
    ADD COLUMN IF NOT EXISTS zotero_annotation_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_notes_zotero_annotation
    ON paper_notes(paper_id, zotero_annotation_key)
    WHERE zotero_annotation_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_paper_notes_paper_source
    ON paper_notes(paper_id, source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_notes_search
    ON paper_notes
    USING GIN (to_tsvector('english', coalesce(user_note, '') || ' ' || coalesce(highlight_text, '')));

COMMIT;
