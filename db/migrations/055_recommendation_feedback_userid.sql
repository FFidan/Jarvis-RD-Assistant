-- Migration 055: Fix W2-9 / W2-10 — align recommendation_feedback.user_id type
-- with the rest of the schema (INTEGER convention) and add a partial index for
-- user-scoped lookups.
--
-- Background: migration 049 created user_id as BIGINT, diverging from the
-- INTEGER NULL convention used by all other per-user columns (papers.user_id,
-- pulse_decks.user_id, journal_entries.user_id, etc.).
--
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction. The
-- migration runner wraps each migration in a transaction (asyncpg savepoint),
-- so CONCURRENTLY is omitted here. The table is small; a plain CREATE INDEX
-- is safe and fast.

ALTER TABLE recommendation_feedback
    ALTER COLUMN user_id TYPE INTEGER USING user_id::integer;

CREATE INDEX IF NOT EXISTS ix_recommendation_feedback_user_id
    ON recommendation_feedback(user_id) WHERE user_id IS NOT NULL;
