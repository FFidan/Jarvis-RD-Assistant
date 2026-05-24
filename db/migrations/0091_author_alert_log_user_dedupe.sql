-- 0091: include user_id in author_alert_log dedupe key.
-- Previously the unique constraint was (tracked_author_id, paper_id), which
-- means one user's alert suppresses the same notification for every other user.
-- The user_id column itself already exists in the baseline (db/init.sql:497);
-- this migration only swaps the unique constraint to a 3-column unique index.

ALTER TABLE author_alert_log
    ADD COLUMN IF NOT EXISTS user_id integer REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE author_alert_log
    DROP CONSTRAINT IF EXISTS author_alert_log_tracked_author_id_paper_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS author_alert_log_dedupe
    ON author_alert_log (tracked_author_id, paper_id, user_id);
