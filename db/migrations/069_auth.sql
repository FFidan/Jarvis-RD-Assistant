-- 069_auth.sql — Phase 2 WS-2A auth foundation: users, magic_link_tokens, sessions.
-- (transaction wrapper added by migrations runner; do not include BEGIN/COMMIT)
--
-- Schema design choice: users.id is BIGSERIAL (INTEGER-compatible) rather than UUID.
-- Rationale: every existing per-tenant column added in Wave-3 (migrations 055, 060,
-- 062-066) is `user_id INTEGER`. Making users.id INTEGER lets those columns be
-- referenced/joined directly without a UUID->INTEGER coordination layer. The
-- session_id (UUID) and magic-link token_hash (TEXT) still use unguessable
-- random tokens for security; only the user identity itself is INTEGER.

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS magic_link_tokens (
    token_hash TEXT PRIMARY KEY,           -- SHA-256 hex digest of the random token in the email link
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_magic_link_tokens_user_expires
    ON magic_link_tokens (user_id, expires_at);

CREATE INDEX IF NOT EXISTS idx_sessions_user_expires
    ON sessions (user_id, expires_at)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_users_email_active
    ON users (email)
    WHERE deleted_at IS NULL;
