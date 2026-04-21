-- Migration 031: Zotero integration
-- Adds Zotero linkage columns to papers and projects,
-- and seeds user_config with Zotero settings keys.

ALTER TABLE papers ADD COLUMN IF NOT EXISTS zotero_item_key TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS zotero_citation_key TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS zotero_last_pushed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS papers_zotero_item_key_idx
    ON papers(zotero_item_key)
    WHERE zotero_item_key IS NOT NULL;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS zotero_collection_key TEXT;

INSERT INTO user_config (key, value) VALUES
    ('zotero.enabled', 'false'::jsonb),
    ('zotero.api_key', '""'::jsonb),
    ('zotero.user_id', '""'::jsonb),
    ('zotero.library_type', '"user"'::jsonb),
    ('zotero.poll_enabled', 'false'::jsonb),
    ('zotero.poll_cron', '"0 * * * *"'::jsonb)
ON CONFLICT (key) DO NOTHING;
