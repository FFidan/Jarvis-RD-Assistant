-- 080_user_deletion_cascade.sql — WS-USER-DELETION (D2: hard cascade after grace)
--
-- Migration 077 added <table>_user_id_fkey constraints with ON DELETE SET
-- NULL on 18 tables. WS-USER-DELETION changes the deletion model: a
-- soft-deleted user is hard-purged after a 30-day grace (jobs/data_purge.py),
-- and the FK cascade collapses all owned rows. For that to be correct the FKs
-- must be ON DELETE CASCADE, and every owned row must have a NON-NULL owner —
-- a NULL user_id would survive a CASCADE delete and become an orphan with no
-- owner. So this migration REFUSES to run while any covered table still holds
-- NULL-user rows (remediate with scripts/migrate_null_user_data.py first).
--
-- SPECIAL CASE — papers.discovered_by:
--   mig 072 renamed papers.user_id -> papers.discovered_by (canonical-corpus
--   model). A NULL discovered_by is an INTENTIONAL shared/system paper, not an
--   orphan (see mig 077 lines 19-32 and the e62ecd72 fix). papers is therefore
--   NOT cascade-deleted and NOT NULL-checked here: its FK stays ON DELETE SET
--   NULL so that purging a user releases their discovered papers back to the
--   shared corpus instead of destroying canonical-corpus rows.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS then ADD inside the canonical
-- DO $$ ... EXCEPTION WHEN duplicate_object guard (migration 051 pattern).
-- No BEGIN/COMMIT (the runner wraps each migration in a transaction).

-- ===== Pre-flight: refuse on NULL-user rows in the 17 cascade tables =====
-- papers is deliberately excluded (NULL discovered_by == shared system paper).
DO $$
DECLARE
    t TEXT;
    n BIGINT;
    cascade_tables TEXT[] := ARRAY[
        'paper_notes', 'paper_summaries', 'paper_chunks', 'paper_user_state',
        'pulse_cards', 'paper_contradictions', 'paper_extractions', 'daily_log',
        'paper_recommendations', 'projects', 'tasks', 'milestones', 'cards',
        'decks', 'review_logs', 'tracked_authors', 'author_alert_log'
    ];
BEGIN
    FOREACH t IN ARRAY cascade_tables LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE user_id IS NULL', t) INTO n;
        IF n > 0 THEN
            RAISE EXCEPTION
                'Migration 080 aborted: table % has % NULL-user row(s). '
                'ON DELETE CASCADE requires non-NULL FK ownership. '
                'Resolve with: python scripts/migrate_null_user_data.py '
                '(see scripts/audit_null_user_data.py for a read-only report).',
                t, n;
        END IF;
    END LOOP;
END $$;

-- ===== paper_notes =====
ALTER TABLE paper_notes DROP CONSTRAINT IF EXISTS paper_notes_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE paper_notes
        ADD CONSTRAINT paper_notes_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_summaries =====
ALTER TABLE paper_summaries DROP CONSTRAINT IF EXISTS paper_summaries_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE paper_summaries
        ADD CONSTRAINT paper_summaries_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_chunks =====
ALTER TABLE paper_chunks DROP CONSTRAINT IF EXISTS paper_chunks_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE paper_chunks
        ADD CONSTRAINT paper_chunks_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_user_state =====
ALTER TABLE paper_user_state DROP CONSTRAINT IF EXISTS paper_user_state_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE paper_user_state
        ADD CONSTRAINT paper_user_state_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== pulse_cards =====
ALTER TABLE pulse_cards DROP CONSTRAINT IF EXISTS pulse_cards_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE pulse_cards
        ADD CONSTRAINT pulse_cards_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_contradictions =====
ALTER TABLE paper_contradictions DROP CONSTRAINT IF EXISTS paper_contradictions_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE paper_contradictions
        ADD CONSTRAINT paper_contradictions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_extractions =====
ALTER TABLE paper_extractions DROP CONSTRAINT IF EXISTS paper_extractions_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE paper_extractions
        ADD CONSTRAINT paper_extractions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== daily_log =====
ALTER TABLE daily_log DROP CONSTRAINT IF EXISTS daily_log_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE daily_log
        ADD CONSTRAINT daily_log_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_recommendations =====
ALTER TABLE paper_recommendations DROP CONSTRAINT IF EXISTS paper_recommendations_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE paper_recommendations
        ADD CONSTRAINT paper_recommendations_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== projects =====
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE projects
        ADD CONSTRAINT projects_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== tasks =====
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE tasks
        ADD CONSTRAINT tasks_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== milestones =====
ALTER TABLE milestones DROP CONSTRAINT IF EXISTS milestones_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE milestones
        ADD CONSTRAINT milestones_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== cards =====
ALTER TABLE cards DROP CONSTRAINT IF EXISTS cards_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE cards
        ADD CONSTRAINT cards_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== decks =====
ALTER TABLE decks DROP CONSTRAINT IF EXISTS decks_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE decks
        ADD CONSTRAINT decks_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== review_logs =====
ALTER TABLE review_logs DROP CONSTRAINT IF EXISTS review_logs_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE review_logs
        ADD CONSTRAINT review_logs_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== tracked_authors =====
ALTER TABLE tracked_authors DROP CONSTRAINT IF EXISTS tracked_authors_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE tracked_authors
        ADD CONSTRAINT tracked_authors_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== author_alert_log =====
ALTER TABLE author_alert_log DROP CONSTRAINT IF EXISTS author_alert_log_user_id_fkey;
DO $$ BEGIN
    ALTER TABLE author_alert_log
        ADD CONSTRAINT author_alert_log_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
