-- 060_daily_intent_user_id_to_int.sql
-- Pre-launch single-user; safe to TRUNCATE. Data lost: zero (no production traffic yet).
-- Downgrade: this migration cannot be reversed (TRUNCATE is destructive). If reverting,
-- restore from backup-pre-060 snapshot (see scripts/backup.sh).
-- Aligns daily_intent.user_id with the project-wide INTEGER NULL convention used by
-- migration 042's user_ownership_columns and the rest of the user-scoped tables.

TRUNCATE TABLE daily_intent;

-- The existing PK is (user_id, intent_date); drop it so we can change the column type.
ALTER TABLE daily_intent DROP CONSTRAINT IF EXISTS daily_intent_pkey;

ALTER TABLE daily_intent ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE daily_intent ALTER COLUMN user_id TYPE INTEGER USING NULL;

-- New unique constraint with NULL cohabitation (PG 15+ NULLS NOT DISTINCT).
CREATE UNIQUE INDEX IF NOT EXISTS daily_intent_user_date_uniq
    ON daily_intent (user_id, intent_date) NULLS NOT DISTINCT;
