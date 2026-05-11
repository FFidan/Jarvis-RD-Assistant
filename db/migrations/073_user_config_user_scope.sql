-- Migration 073: scope user_config rows by optional user_id.
--
-- NULL user_id rows remain system/default rows for existing single-user
-- installs. Authenticated browser users can override personal keys in
-- (user_id, key) rows while system-scoped keys continue to use user_id=NULL.

ALTER TABLE user_config ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE user_config ALTER COLUMN value DROP NOT NULL;
ALTER TABLE user_config ADD COLUMN IF NOT EXISTS encrypted_value BYTEA NULL;

DO $$
BEGIN
    ALTER TABLE user_config
        ADD CONSTRAINT user_config_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN NULL;
END $$;

ALTER TABLE user_config DROP CONSTRAINT IF EXISTS user_config_key_key;
DROP INDEX IF EXISTS user_config_key_key;

CREATE UNIQUE INDEX IF NOT EXISTS user_config_user_key_idx
    ON user_config (user_id, key) NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS idx_user_config_user_id
    ON user_config(user_id);
