-- 0104: paper_chunks are canonical paper data, not user-owned records.
-- A user deletion must never cascade into chunks retained by another library.
ALTER TABLE IF EXISTS paper_chunks
    DROP CONSTRAINT IF EXISTS paper_chunks_user_id_fkey;

DROP INDEX IF EXISTS idx_paper_chunks_user;

ALTER TABLE IF EXISTS paper_chunks
    DROP COLUMN IF EXISTS user_id;
