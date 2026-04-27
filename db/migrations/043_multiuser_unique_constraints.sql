-- Wave 6 multi-tenant unique-constraint fixup + pulse_decks/pulse_cards user_id.
-- (migrations_runner.py wraps every migration in a savepoint — no BEGIN/COMMIT here.)
--
-- C3/WS-6D: Replace per-paper UNIQUE constraints with (paper_id, user_id) so two
-- users can independently track the same paper without violating the unique index.
-- Uses UNIQUE NULLS NOT DISTINCT so (paper_id=1, user_id=NULL) and
-- (paper_id=1, user_id=42) coexist while (paper_id=1, user_id=NULL) is still
-- unique among "no owner" rows.  Requires PostgreSQL 15+ (project uses PG 16).
--
-- C2 doc: Add nullable user_id to pulse_decks and pulse_cards so future multi-tenant
-- generation can segregate decks per user.  NULL = system/single-tenant deck.

-- =============================================================================
-- paper_user_state: drop single-paper UNIQUE, add (paper_id, user_id)
-- =============================================================================

-- The UNIQUE constraint on paper_id was created inline as part of the column
-- definition so PostgreSQL auto-named it paper_user_state_paper_id_key.
ALTER TABLE paper_user_state
    DROP CONSTRAINT IF EXISTS paper_user_state_paper_id_key;

ALTER TABLE paper_user_state
    ADD CONSTRAINT paper_user_state_paper_id_user_id_key
    UNIQUE NULLS NOT DISTINCT (paper_id, user_id);

-- =============================================================================
-- paper_summaries: drop single-paper UNIQUE, add (paper_id, user_id)
-- =============================================================================

-- Auto-named by PostgreSQL from the UNIQUE column constraint: paper_summaries_paper_id_key.
ALTER TABLE paper_summaries
    DROP CONSTRAINT IF EXISTS paper_summaries_paper_id_key;

ALTER TABLE paper_summaries
    ADD CONSTRAINT paper_summaries_paper_id_user_id_key
    UNIQUE NULLS NOT DISTINCT (paper_id, user_id);

-- =============================================================================
-- paper_topics: already has PRIMARY KEY (paper_id, topic_id) — no single-paper
-- UNIQUE exists; verify and leave as-is.  No changes needed.
-- =============================================================================

-- =============================================================================
-- pulse_decks: add nullable user_id (C2 doc — multi-tenant deck segregation)
-- =============================================================================

ALTER TABLE pulse_decks
    ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;

-- Drop the pre-existing single-user UNIQUE on deck_date before replacing it with
-- a per-user unique so multiple users can each have a deck for the same date.
ALTER TABLE pulse_decks
    DROP CONSTRAINT IF EXISTS pulse_decks_deck_date_key;

-- Per-user deck_date uniqueness: (deck_date, user_id) with NULLS NOT DISTINCT
-- so that single-tenant rows (user_id=NULL) deduplicate correctly.
ALTER TABLE pulse_decks
    ADD CONSTRAINT pulse_decks_deck_date_user_id_key
    UNIQUE NULLS NOT DISTINCT (deck_date, user_id);

CREATE INDEX IF NOT EXISTS idx_pulse_decks_user
    ON pulse_decks(user_id) WHERE user_id IS NOT NULL;

-- =============================================================================
-- pulse_cards: add nullable user_id (C2 doc)
-- =============================================================================

ALTER TABLE pulse_cards
    ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;

CREATE INDEX IF NOT EXISTS idx_pulse_cards_user_043
    ON pulse_cards(user_id) WHERE user_id IS NOT NULL;
