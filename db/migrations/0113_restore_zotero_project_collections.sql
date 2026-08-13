-- Restore the project-to-Zotero collection cache used by the push workflow.
-- Older installations that applied migration 031 already have this column;
-- the IF NOT EXISTS keeps those valid upgrade paths idempotent.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS zotero_collection_key TEXT;

COMMENT ON COLUMN projects.zotero_collection_key IS
    'Collection key for this project in its owner''s active Zotero library. Cleared when that library identity changes and resolved again on the next project-linked push.';
