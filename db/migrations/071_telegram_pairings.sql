-- 071_telegram_pairings.sql — Sprint A (Telegram chat→user pairing)
--
-- Introduces per-user Telegram pairing so every user can pair their own
-- Telegram chat independently.  Previously the single-tenant setup stored one
-- chat ID in user_config(key='telegram.owner_chat_id').
--
-- New tables:
--   telegram_pairing_tokens  — short-lived tokens issued by the dashboard;
--                              consumed by the bot /pair command.
--   telegram_user_pairings   — permanent per-user chat-id registry.
--
-- The legacy telegram_pairing table (setup-wizard single-use codes) and
-- user_config rows are intentionally kept intact so existing single-tenant
-- pairings continue working until users re-pair via the new flow.
--
-- (Transaction wrapper added by the migrations runner; do not include
-- BEGIN/COMMIT here.)

CREATE TABLE IF NOT EXISTS telegram_pairing_tokens (
    token         TEXT         NOT NULL PRIMARY KEY,
    user_id       BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ  NOT NULL,
    consumed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_telegram_pairing_tokens_user
    ON telegram_pairing_tokens(user_id);

CREATE TABLE IF NOT EXISTS telegram_user_pairings (
    user_id           BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_id           BIGINT       NOT NULL,
    telegram_username TEXT,
    paired_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id),
    UNIQUE (chat_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_user_pairings_chat
    ON telegram_user_pairings(chat_id);
