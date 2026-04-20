-- 028: drop legacy papers.is_read column (superseded by paper_user_state.status)
BEGIN;

DROP INDEX IF EXISTS idx_papers_unread;
ALTER TABLE papers DROP COLUMN IF EXISTS is_read;

COMMIT;
