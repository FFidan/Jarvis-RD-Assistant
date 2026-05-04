-- Phase 2 My Day: journal_entries table for end-of-day reflection prompts.
--
-- user_id follows the INTEGER NULL convention used by all other per-user tables
-- in this schema (papers, pulse_decks, etc.).  NULLS NOT DISTINCT ensures a
-- single-tenant NULL user_id deduplicates by date just like a real user would.

CREATE TABLE IF NOT EXISTS journal_entries (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER,
    date        DATE NOT NULL DEFAULT CURRENT_DATE,
    prompts     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-user-per-date uniqueness (NULLS NOT DISTINCT so single-tenant NULL rows
-- still deduplicate correctly).
DO $$ BEGIN
    ALTER TABLE journal_entries
        ADD CONSTRAINT journal_entries_user_id_date_key
        UNIQUE NULLS NOT DISTINCT (user_id, date);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
