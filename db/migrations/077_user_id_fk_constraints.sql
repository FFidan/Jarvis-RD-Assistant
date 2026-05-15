-- 077_user_id_fk_constraints.sql — Wave 1 Task 1.4 / H4
--
-- Adds missing FOREIGN KEY constraints on 18 tables whose `user_id` columns
-- were added by migrations 042, 062-066, and 070 WITHOUT a REFERENCES clause.
-- Later migrations (072-076) consistently use REFERENCES users(id) — this
-- migration brings the earlier columns up to the same standard.
--
-- Pattern per table:
--   1. NULL out any orphan user_ids that point to non-existent users (so the
--      FK can be added without violation).
--   2. DO $$ BEGIN … ADD CONSTRAINT … EXCEPTION WHEN duplicate_object THEN
--      NULL; END $$ — the canonical idempotent ADD CONSTRAINT pattern from
--      migration 051. Safe to re-apply (duplicate_object is swallowed).
--
-- ON DELETE SET NULL matches the migs 072-076 convention for non-CASCADE
-- user-scoped data tables; rows survive a user deletion as system-owned
-- (project convention from 042/070: NULL user_id = visible to all users).

-- ===== papers =====
-- mig 072 renamed papers.user_id -> papers.discovered_by (canonical_corpus model)
-- so the FK lives on discovered_by, not user_id.
UPDATE papers
   SET discovered_by = NULL
 WHERE discovered_by IS NOT NULL
   AND discovered_by NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE papers
        ADD CONSTRAINT papers_discovered_by_fkey
        FOREIGN KEY (discovered_by) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_notes =====
UPDATE paper_notes
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE paper_notes
        ADD CONSTRAINT paper_notes_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_summaries =====
UPDATE paper_summaries
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE paper_summaries
        ADD CONSTRAINT paper_summaries_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_chunks =====
UPDATE paper_chunks
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE paper_chunks
        ADD CONSTRAINT paper_chunks_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_user_state =====
UPDATE paper_user_state
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE paper_user_state
        ADD CONSTRAINT paper_user_state_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== pulse_cards =====
UPDATE pulse_cards
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE pulse_cards
        ADD CONSTRAINT pulse_cards_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_contradictions =====
UPDATE paper_contradictions
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE paper_contradictions
        ADD CONSTRAINT paper_contradictions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_extractions =====
UPDATE paper_extractions
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE paper_extractions
        ADD CONSTRAINT paper_extractions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== daily_log =====
UPDATE daily_log
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE daily_log
        ADD CONSTRAINT daily_log_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== paper_recommendations =====
UPDATE paper_recommendations
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE paper_recommendations
        ADD CONSTRAINT paper_recommendations_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== projects =====
UPDATE projects
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE projects
        ADD CONSTRAINT projects_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== tasks =====
UPDATE tasks
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE tasks
        ADD CONSTRAINT tasks_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== milestones =====
UPDATE milestones
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE milestones
        ADD CONSTRAINT milestones_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== cards =====
UPDATE cards
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE cards
        ADD CONSTRAINT cards_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== decks =====
UPDATE decks
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE decks
        ADD CONSTRAINT decks_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== review_logs =====
UPDATE review_logs
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE review_logs
        ADD CONSTRAINT review_logs_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== tracked_authors =====
UPDATE tracked_authors
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE tracked_authors
        ADD CONSTRAINT tracked_authors_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ===== author_alert_log =====
UPDATE author_alert_log
   SET user_id = NULL
 WHERE user_id IS NOT NULL
   AND user_id NOT IN (SELECT id FROM users);

DO $$ BEGIN
    ALTER TABLE author_alert_log
        ADD CONSTRAINT author_alert_log_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
