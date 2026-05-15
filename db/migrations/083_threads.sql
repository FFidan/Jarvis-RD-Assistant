-- 083_threads.sql — UI_v3 My-Day: `thread` entity (greenfield).
--
-- A `thread` is a user's resumable mid-flight line of work surfaced in My-Day's
-- § Open threads section and the 3-mode hero ("Resume thread"). It is both
-- user-created AND auto-seeded from (a) interrupted Pomodoro sessions and
-- (b) the EOD "make this a thread" action (spec §3.10 / §4.1, open-question 2
-- RESOLVED 2026-05-15).
--
-- user_id follows the INTEGER NULL convention used by every other per-user
-- table in this schema (papers, journal_entries, daily_intent, …). The FK is
-- ON DELETE CASCADE (owned-data rows) — consistent with migration 082's
-- treatment of journal_entries / daily_intent.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS plus the canonical
-- DO $$ … EXCEPTION WHEN duplicate_object guard (migration 051 pattern) for the
-- FK constraint. No BEGIN/COMMIT (the runner wraps each migration in a
-- transaction).

CREATE TABLE IF NOT EXISTS thread (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER,
    title       TEXT NOT NULL,
    anchor      TEXT,
    progress    REAL NOT NULL DEFAULT 0
                CHECK (progress >= 0 AND progress <= 1),
    last_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'done', 'archived')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-user lookup index (matches the partial-index convention used by tasks /
-- projects in init.sql). Drives the My-Day list query (user-scoped, ordered by
-- last_at DESC).
CREATE INDEX IF NOT EXISTS idx_thread_user
    ON thread (user_id, last_at DESC)
    WHERE user_id IS NOT NULL;

-- FK → users(id) ON DELETE CASCADE (owned-data rows; mirrors migration 082's
-- discipline for journal_entries / daily_intent).
DO $$ BEGIN
    ALTER TABLE thread
        ADD CONSTRAINT thread_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
