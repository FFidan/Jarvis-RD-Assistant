-- 0102: WebAuthn/passkey credential storage.
--
-- Stores registered passkeys (webauthn_credentials), short-lived single-use
-- ceremony nonces (webauthn_challenges), and links a browser session to the
-- passkey it was minted from (sessions.credential_id). The migration runner
-- wraps this file in a transaction and strips any outer BEGIN/COMMIT, so none
-- appear here. All statements are idempotent (IF NOT EXISTS) for safe re-apply.
--
-- FK column types match the baseline: users.id and sessions.user_id are bigint;
-- sessions.id (and thus webauthn_credentials.id) is uuid.

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id bytea UNIQUE NOT NULL,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL DEFAULT 0,
    transports text[],
    aaguid uuid,
    nickname text,
    created_at timestamptz DEFAULT now(),
    last_used_at timestamptz
);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    challenge bytea PRIMARY KEY,
    user_id bigint REFERENCES users(id) ON DELETE CASCADE,
    purpose text NOT NULL,
    expires_at timestamptz NOT NULL
);

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS credential_id uuid
        REFERENCES webauthn_credentials(id) ON DELETE SET NULL;
