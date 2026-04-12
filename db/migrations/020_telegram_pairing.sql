-- Migration 020: telegram pairing + setup status seed keys
-- Adds a short-lived pairing code table used by the setup wizard to link
-- a Telegram chat to the JARVIS owner, and seeds the user_config keys that
-- track owner chat id and wizard completion.

CREATE TABLE IF NOT EXISTS telegram_pairing (
    code        TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS telegram_pairing_expires_idx
    ON telegram_pairing (expires_at);

INSERT INTO user_config (key, value) VALUES
    ('telegram.owner_chat_id', 'null'::jsonb),
    ('setup.completed',        'false'::jsonb)
ON CONFLICT (key) DO NOTHING;
