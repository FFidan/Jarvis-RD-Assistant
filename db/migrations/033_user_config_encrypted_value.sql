-- Migration 033: add encrypted_value column to user_config for at-rest secret storage.
-- Existing plaintext rows keep their `value` populated; new writes to encrypted keys
-- populate `encrypted_value` and leave `value` NULL. Lazy re-save by the user migrates
-- existing rows. No data rewrite in this migration.

ALTER TABLE user_config ADD COLUMN IF NOT EXISTS encrypted_value BYTEA NULL;
