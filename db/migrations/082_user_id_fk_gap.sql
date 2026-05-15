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
-- For the CASCADE tables we also verify no NULL rows remain before adding the
-- constraint, mirroring 080's discipline. For pulse_models the FK is SET NULL
-- so NULL rows are intentionally allowed.
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

-- ===== Pre-flight: refuse on NULL-user rows in the 6 CASCADE tables =====
-- (mirrors 080's pre-flight discipline; pulse_models is excluded because
--  NULL user_id = shared/system model, so SET NULL is used instead.)
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
            RAISE EXCEPTION
                'Migration 082 aborted: table % has % NULL-user row(s). '
                'ON DELETE CASCADE requires non-NULL FK ownership. '
                'Resolve with: python scripts/migrate_null_user_data.py '
                '(see scripts/audit_null_user_data.py for a read-only report).',
                t, n;
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
