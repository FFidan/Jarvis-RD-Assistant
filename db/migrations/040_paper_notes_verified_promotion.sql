-- Migration 037: verified promotion state for Zotero annotation notes.
BEGIN;

ALTER TABLE paper_notes
    ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'verified', 'failed')),
    ADD COLUMN IF NOT EXISTS verified_quote TEXT,
    ADD COLUMN IF NOT EXISTS verified_page_number INTEGER,
    ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_paper_notes_verification_status
    ON paper_notes(paper_id, source, verification_status);

COMMIT;
