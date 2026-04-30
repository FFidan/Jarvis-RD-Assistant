-- Migration 047 — collapse paper_user_state lifecycle to single-state ENUM.
-- Spec: docs/specs/2026-04-29-paper-lifecycle-redesign.md §3.1
--
-- Replaces 5 booleans + 1 status enum (saved/dismissed/archived/status/preference)
-- with one `state` ENUM ('inbox','to_read','reading','done','trash') plus a
-- `state_before_trash` ENUM column to support Restore from Trash.
--
-- Backfill is deterministic: every legal pre-redesign combination maps to exactly
-- one new state. The mapping order matters (dismissed wins over archived/read,
-- which win over reading, which wins over saved). See spec §3.1 for the rationale.

-- Add new state columns. NOT NULL DEFAULT 'inbox' so existing rows get a safe
-- default before the backfill runs and rewrites them.
ALTER TABLE paper_user_state
    ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'inbox'
        CHECK (state IN ('inbox', 'to_read', 'reading', 'done', 'trash')),
    ADD COLUMN IF NOT EXISTS state_before_trash TEXT
        CHECK (state_before_trash IS NULL
               OR state_before_trash IN ('inbox', 'to_read', 'reading', 'done'));

-- Backfill state from legacy booleans + status. Order is significant:
--   dismissed=TRUE          → trash (highest priority — explicit user rejection)
--   archived=TRUE OR
--      status='read'        → done  (finished or kept-out-of-active-view)
--   status='reading'        → reading
--   saved=TRUE              → to_read (explicitly saved, not yet engaged)
--   else                    → inbox  (untriaged or no-row-default)
UPDATE paper_user_state SET state = CASE
    WHEN dismissed = TRUE                       THEN 'trash'
    WHEN archived = TRUE OR status = 'read'     THEN 'done'
    WHEN status = 'reading'                     THEN 'reading'
    WHEN saved = TRUE                           THEN 'to_read'
    ELSE 'inbox'
END;

-- For trash rows, populate state_before_trash so Restore works deterministically.
-- The CASE deliberately ignores the `dismissed=TRUE` branch (since dismissed
-- rows are already 'trash'); it computes what the state WOULD HAVE BEEN if the
-- paper were not dismissed.
UPDATE paper_user_state SET state_before_trash = CASE
    WHEN archived = TRUE OR status = 'read'     THEN 'done'
    WHEN status = 'reading'                     THEN 'reading'
    WHEN saved = TRUE                           THEN 'to_read'
    ELSE 'inbox'
END
WHERE state = 'trash';

-- Drop legacy columns (the lint guard check-archived-predicate.sh is deleted
-- as the archived predicate is no longer needed).
ALTER TABLE paper_user_state
    DROP COLUMN IF EXISTS saved,
    DROP COLUMN IF EXISTS dismissed,
    DROP COLUMN IF EXISTS archived,
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS preference;

-- Drop the legacy paper_user_state_status_check constraint if it survives the
-- column drop. (Postgres normally drops CHECK constraints when their column
-- goes; this DO block is defensive in case of partial-apply state.)
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

-- Index `state` for view-predicate queries (Inbox / Library / Trash filters).
CREATE INDEX IF NOT EXISTS idx_paper_user_state_state ON paper_user_state(state);

-- Comments for future readers.
COMMENT ON COLUMN paper_user_state.state IS
    'Lifecycle position: inbox (untriaged), to_read (saved), reading (engaging), done (finished), trash (rejected). Replaces 5 booleans + status enum from migration 046.';
COMMENT ON COLUMN paper_user_state.state_before_trash IS
    'For trash rows: the state to restore to. NULL for non-trash rows.';
