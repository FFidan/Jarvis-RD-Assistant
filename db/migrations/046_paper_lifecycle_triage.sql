BEGIN;

-- Triage axis
ALTER TABLE paper_user_state
    ADD COLUMN IF NOT EXISTS saved BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS dismissed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Backfill: existing rows = user has interacted = saved
UPDATE paper_user_state SET saved = TRUE WHERE saved IS NOT TRUE;

-- Migrate legacy status='starred' → starred=TRUE, status='read', saved=TRUE
UPDATE paper_user_state
   SET starred = TRUE, status = 'read', saved = TRUE
 WHERE status = 'starred';

-- Migrate legacy status='archived' → archived=TRUE, status='read', saved=TRUE
UPDATE paper_user_state
   SET archived = TRUE, status = 'read', saved = TRUE
 WHERE status = 'archived';

-- Drop legacy enum values from CHECK; defensive lookup mirroring migration 043 pattern
DO $$
DECLARE cons_name TEXT;
BEGIN
    FOR cons_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'paper_user_state'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE paper_user_state DROP CONSTRAINT IF EXISTS %I', cons_name);
    END LOOP;
END $$;

ALTER TABLE paper_user_state
    ADD CONSTRAINT paper_user_state_status_check
    CHECK (status IN ('new', 'reading', 'read'));

-- Maintain updated_at (reuse helper from migration 042)
DROP TRIGGER IF EXISTS set_updated_at_paper_user_state ON paper_user_state;
CREATE TRIGGER set_updated_at_paper_user_state
    BEFORE UPDATE ON paper_user_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
