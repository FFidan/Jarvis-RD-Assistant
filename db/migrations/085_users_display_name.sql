-- 085_users_display_name.sql — UI_v3 §I Account: self-service profile + verified email change.
-- (transaction wrapper added by migrations runner; do NOT include BEGIN/COMMIT)
--
-- Two additive, idempotent changes:
--
--   1. users.display_name (nullable TEXT) — the §I Account "Profile" field.
--      Nullable: existing users have no display name; the UI falls back to
--      the email local-part. No backfill, no default.
--
--   2. magic_link_tokens.pending_email (nullable TEXT) — overloads the
--      existing single-use / 15-min-expiry / SHA-256-hashed token table for
--      the verified email-change flow. When pending_email IS NOT NULL the
--      token is an email-change confirmation (swap users.email on consume)
--      rather than a login token. Reusing this table means the email-change
--      path inherits the audited, atomic, replay-safe consume logic in
--      /api/auth/verify — no new crypto, no second token store.
--
-- Both use ADD COLUMN IF NOT EXISTS so re-applying the migration on an
-- already-migrated database is a no-op.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS display_name TEXT;

ALTER TABLE magic_link_tokens
    ADD COLUMN IF NOT EXISTS pending_email TEXT;
