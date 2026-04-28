-- 044: separate per-user saved/archive/preference flags from reading status.

ALTER TABLE paper_user_state
    ADD COLUMN IF NOT EXISTS starred BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE paper_user_state
    ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE paper_user_state
    ADD COLUMN IF NOT EXISTS preference VARCHAR(10) NOT NULL DEFAULT 'none';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'paper_user_state_preference_check'
          AND conrelid = 'paper_user_state'::regclass
    ) THEN
        ALTER TABLE paper_user_state
            ADD CONSTRAINT paper_user_state_preference_check
            CHECK (preference IN ('none', 'up', 'down'));
    END IF;
END $$;

UPDATE paper_user_state
SET starred = TRUE
WHERE status = 'starred';

UPDATE paper_user_state
SET archived = TRUE
WHERE status = 'archived';

COMMENT ON COLUMN paper_user_state.status IS
    'Reading state. Legacy archived/starred values remain accepted for compatibility.';
COMMENT ON COLUMN paper_user_state.starred IS
    'Per-user saved/bookmarked flag independent from reading status.';
COMMENT ON COLUMN paper_user_state.archived IS
    'Per-user archive flag independent from reading status.';
COMMENT ON COLUMN paper_user_state.preference IS
    'Current per-user paper preference: none, up, or down.';
