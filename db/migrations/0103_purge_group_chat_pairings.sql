-- Purge stale group/supergroup telegram pairings (chat_id < 0) created before the
-- private-chat-only pairing guard; they still receive outbound scheduled pushes.
DELETE FROM telegram_user_pairings WHERE chat_id < 0;
