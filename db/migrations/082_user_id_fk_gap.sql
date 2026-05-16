-- 082_user_id_fk_gap.sql — H-3: add user_id FK on 7 tables missed by 077/080
--
-- Migrations 077 and 080 covered 18 tables but omitted 7 tables that have a
-- user_id column with no REFERENCES users(id):
--   pulse_decks, recommendation_feedback, source_health, source_run_history,
--   daily_intent, journal_entries  — ON DELETE CASCADE (owned-data rows)
--   pulse_models                   — ON DELETE SET NULL (NULL = shared/system model)
--
-- All 7 columns are nullable (INTEGER NULL) so the pre-flight follows 080's
-- pattern: null-out / delete true orphans first, then add the FK idempotently.
--
-- Pre-flight: RAISE on any remaining true orphan rows (user_id NOT NULL but
-- pointing to a non-existent users row) so the ADD CONSTRAINT cannot fail.
-- For the CASCADE tables we self-heal any NULL-user rows in-line (see the
-- self-heal block below) instead of aborting, mirroring 080's discipline. For
-- pulse_models the FK is SET NULL so NULL rows are intentionally allowed.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS then ADD inside the canonical
-- DO $$ ... EXCEPTION WHEN duplicate_object guard (migration 051 pattern).
-- No BEGIN/COMMIT (the runner wraps each migration in a transaction).

-- ===== Pre-flight: null-out orphan user_ids (user_id NOT IN users) =====
UPDATE pulse_decks
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

UPDATE recommendation_feedback
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

UPDATE source_health
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

UPDATE source_run_history
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

UPDATE daily_intent
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

UPDATE journal_entries
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

UPDATE pulse_models
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

-- ===== Pre-flight: self-heal NULL-user rows in the 6 CASCADE tables =====
-- An earlier revision RAISEd here, hard-aborting the migration runner on any
-- pre-v0.4.0 upgrade that still held NULL-user rows. Because the runner retries
-- the whole chain on every boot, that turned a one-time data condition into a
-- crash-loop. Instead we self-heal in-line, mirroring the SAFE strategy of
-- scripts/migrate_null_user_data.py (single transaction — the runner's —
-- per-table backup, then reassign-to-first-admin, NEVER delete):
--   1. If a table has NULL-user rows, snapshot them into
--      <table>_null_user_backup_082 (CREATE TABLE IF NOT EXISTS, so a retried
--      migration never clobbers an existing backup).
--   2. Reassign those rows to the oldest active admin
--      (role='admin' AND deleted_at IS NULL, ORDER BY created_at, id LIMIT 1).
-- Idempotent / safe edge cases:
--   * No NULL rows  -> 0-row no-op, no backup table created.
--   * No admin yet  -> the scalar sub-select is NULL, the UPDATE matches the
--     same NULL rows and re-sets user_id = NULL (still 0 effective change);
--     ADD CONSTRAINT on a nullable column tolerates NULLs, so a fresh install
--     with no users is unaffected. The follow-up FK still enforces validity for
--     any non-NULL value.
-- Operators with non-trivial cases (delete vs reassign, multi-admin routing)
-- should run scripts/migrate_null_user_data.py BEFORE upgrading; this in-line
-- heal is the safe automatic fallback, not a replacement for that tool.
-- (pulse_models is excluded — NULL user_id = shared/system model, SET NULL.)
DO $$
DECLARE
    t TEXT;
    n BIGINT;
    cascade_tables TEXT[] := ARRAY[
        'pulse_decks', 'recommendation_feedback', 'source_health',
        'source_run_history', 'daily_intent', 'journal_entries'
    ];
BEGIN
    FOREACH t IN ARRAY cascade_tables LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE user_id IS NULL', t) INTO n;
        IF n > 0 THEN
            -- 1. Per-table backup of the affected rows (idempotent).
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I AS '
                'SELECT * FROM %I WHERE user_id IS NULL',
                t || '_null_user_backup_082', t
            );
            -- 2. Reassign to the oldest active admin (NULL if none -> no-op).
            EXECUTE format(
                'UPDATE %I SET user_id = ('
                '  SELECT id FROM users'
                '   WHERE role = ''admin'' AND deleted_at IS NULL'
                '   ORDER BY created_at ASC, id ASC LIMIT 1'
                ') WHERE user_id IS NULL', t
            );
        END IF;
    END LOOP;
END $$;

-- ===== pulse_decks =====
ALTER TABLE pulse_decks DROP CONSTRAINT IF EXISTS pulse_decks_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE pulse_decks
        ADD CONSTRAINT pulse_decks_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== recommendation_feedback =====
ALTER TABLE recommendation_feedback DROP CONSTRAINT IF EXISTS recommendation_feedback_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE recommendation_feedback
        ADD CONSTRAINT recommendation_feedback_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== source_health =====
ALTER TABLE source_health DROP CONSTRAINT IF EXISTS source_health_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE source_health
        ADD CONSTRAINT source_health_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== source_run_history =====
ALTER TABLE source_run_history DROP CONSTRAINT IF EXISTS source_run_history_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE source_run_history
        ADD CONSTRAINT source_run_history_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== daily_intent =====
ALTER TABLE daily_intent DROP CONSTRAINT IF EXISTS daily_intent_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE daily_intent
        ADD CONSTRAINT daily_intent_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== journal_entries =====
ALTER TABLE journal_entries DROP CONSTRAINT IF EXISTS journal_entries_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE journal_entries
        ADD CONSTRAINT journal_entries_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== pulse_models =====
-- ON DELETE SET NULL: NULL user_id = shared/system model, intentionally survives
-- user deletion (same rationale as papers.discovered_by in mig 077/080).
ALTER TABLE pulse_models DROP CONSTRAINT IF EXISTS pulse_models_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE pulse_models
        ADD CONSTRAINT pulse_models_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
