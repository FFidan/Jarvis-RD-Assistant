-- =============================================================================
-- Reconciliation script for dev DBs that booted master before 2026-04-26.
--
-- Background: migrations 037 and 038 were briefly colliding:
--   037_paper_notes_verified_promotion.sql  (now 040)
--   038_jobs_notify.sql                     (now 041)
-- Any dev DB that started after commit 7ac5af3 will have schema_migrations
-- rows for version=37 (pulse_models) and version=38 (paper_contradictions),
-- but NOT for the paper_notes promotion or the jobs NOTIFY trigger.
--
-- This script is IDEMPOTENT — safe to run multiple times on any DB.
-- It adds the missing DDL with IF NOT EXISTS / OR REPLACE guards and
-- registers the missing versions in schema_migrations.
--
-- Usage (from repo root):
--   docker compose exec postgres psql -U jarvis jarvis -f /reconcile_037_038_collision.sql
-- Or mount it as a volume and run via psql inside the container.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 040_paper_notes_verified_promotion: verified promotion state for Zotero notes
-- ---------------------------------------------------------------------------
ALTER TABLE paper_notes
    ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'verified', 'failed')),
    ADD COLUMN IF NOT EXISTS verified_quote TEXT,
    ADD COLUMN IF NOT EXISTS verified_page_number INTEGER,
    ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_paper_notes_verification_status
    ON paper_notes(paper_id, source, verification_status);

-- ---------------------------------------------------------------------------
-- 041_jobs_notify: NOTIFY trigger for real-time job SSE streaming
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION notify_jobs_update()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT'
       OR OLD.status IS DISTINCT FROM NEW.status
       OR OLD.progress IS DISTINCT FROM NEW.progress
       OR OLD.progress_message IS DISTINCT FROM NEW.progress_message
       OR OLD.result IS DISTINCT FROM NEW.result
       OR OLD.error IS DISTINCT FROM NEW.error THEN
        PERFORM pg_notify('jarvis_jobs', NEW.id::text);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobs_notify_update ON jobs;
CREATE TRIGGER trg_jobs_notify_update
AFTER INSERT OR UPDATE OF status, progress, progress_message, result, error ON jobs
FOR EACH ROW EXECUTE FUNCTION notify_jobs_update();

-- ---------------------------------------------------------------------------
-- Mark both versions as applied so migrations_runner skips them on next boot
-- ---------------------------------------------------------------------------
INSERT INTO schema_migrations (version) VALUES (40), (41) ON CONFLICT DO NOTHING;
