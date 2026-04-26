-- Migration 038: NOTIFY listeners when job progress or terminal state changes.
-- Application helpers also call pg_notify after known job updates; this trigger
-- is intentional defense-in-depth for direct SQL updates and older workers.
BEGIN;

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

COMMIT;
