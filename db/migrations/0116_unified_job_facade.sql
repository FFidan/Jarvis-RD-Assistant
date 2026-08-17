-- Platform-owned public job facade and authoritative owner metadata.

SET LOCAL ROLE jarvis_ops_owner;

CREATE TABLE ops.job_owner_registry (
    task_name text PRIMARY KEY,
    queue_name text NOT NULL,
    service_name text NOT NULL CHECK (service_name IN ('research', 'learning')),
    CHECK (
        (service_name = 'research' AND queue_name = 'paper_ingestion')
        OR (service_name = 'learning' AND queue_name = 'learning_engine')
    )
);

INSERT INTO ops.job_owner_registry (task_name, queue_name, service_name) VALUES
    ('paper.process', 'paper_ingestion', 'research'),
    ('paper.analyze', 'paper_ingestion', 'research'),
    ('papers.batch_process', 'paper_ingestion', 'research'),
    ('papers.batch_summarize', 'paper_ingestion', 'research'),
    ('papers.process_library', 'paper_ingestion', 'research'),
    ('papers.scan_local', 'paper_ingestion', 'research'),
    ('paper.summarize', 'paper_ingestion', 'research'),
    ('citations.batch_fetch', 'paper_ingestion', 'research'),
    ('digest.weekly', 'paper_ingestion', 'research'),
    ('extraction.single', 'paper_ingestion', 'research'),
    ('extraction.batch', 'paper_ingestion', 'research'),
    ('contradictions.scan', 'paper_ingestion', 'research'),
    ('pulse.generate', 'paper_ingestion', 'research'),
    ('pulse.train_classifier', 'paper_ingestion', 'research'),
    ('model.pull', 'paper_ingestion', 'research'),
    ('zotero.push', 'paper_ingestion', 'research'),
    ('zotero.resync', 'paper_ingestion', 'research'),
    ('zotero.sync_from_zotero', 'paper_ingestion', 'research'),
    ('zotero.sync_annotations', 'paper_ingestion', 'research'),
    ('zotero.push_highlights', 'paper_ingestion', 'research'),
    ('card.generate', 'learning_engine', 'learning'),
    ('card.generate_batch', 'learning_engine', 'learning')
ON CONFLICT (task_name) DO UPDATE
SET queue_name = EXCLUDED.queue_name, service_name = EXCLUDED.service_name;

ALTER TABLE ops.procrastinate_jobs
    ADD COLUMN IF NOT EXISTS owner_queue text,
    ADD COLUMN IF NOT EXISTS owner_service text;

UPDATE ops.procrastinate_jobs AS job
SET owner_queue = registry.queue_name, owner_service = registry.service_name
FROM ops.job_owner_registry AS registry
WHERE job.task_name = registry.task_name;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM ops.procrastinate_jobs AS job
        WHERE job.args ? 'job_id'
          AND job.status IN ('todo', 'doing')
          AND (job.owner_queue IS NULL OR job.owner_service IS NULL)
    ) THEN
        RAISE EXCEPTION 'active application job has no owner mapping';
    END IF;
END $$;

UPDATE ops.procrastinate_jobs
SET owner_queue = 'legacy_unknown', owner_service = 'legacy_unknown'
WHERE args ? 'job_id'
  AND owner_queue IS NULL
  AND status NOT IN ('todo', 'doing');

CREATE OR REPLACE FUNCTION ops.enforce_job_owner_metadata_v1()
RETURNS trigger LANGUAGE plpgsql
SET search_path = ops, pg_catalog
AS $$
DECLARE owner_record ops.job_owner_registry%ROWTYPE;
BEGIN
    IF NOT (NEW.args ? 'job_id') THEN
        RETURN NEW;
    END IF;
    SELECT * INTO owner_record FROM ops.job_owner_registry WHERE task_name = NEW.task_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'application job task has no owner mapping';
    END IF;
    IF NEW.queue_name <> owner_record.queue_name THEN
        RAISE EXCEPTION 'job queue does not match task owner';
    END IF;
    IF (NEW.owner_queue IS NOT NULL AND NEW.owner_queue <> owner_record.queue_name)
       OR (NEW.owner_service IS NOT NULL AND NEW.owner_service <> owner_record.service_name) THEN
        RAISE EXCEPTION 'job owner metadata does not match task owner';
    END IF;
    NEW.owner_queue := owner_record.queue_name;
    NEW.owner_service := owner_record.service_name;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS procrastinate_jobs_owner_guard_v1 ON ops.procrastinate_jobs;
CREATE TRIGGER procrastinate_jobs_owner_guard_v1
BEFORE INSERT OR UPDATE OF queue_name, task_name, owner_queue, owner_service
ON ops.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION ops.enforce_job_owner_metadata_v1();

CREATE OR REPLACE VIEW ops.jarvis_jobs_rollback_v1 AS
SELECT args->>'job_id' AS id, task_name AS kind, args->>'user_id' AS user_id,
       args - 'job_id' - 'user_id' AS payload, status::text AS raw_status
FROM ops.procrastinate_jobs WHERE args ? 'job_id';

CREATE OR REPLACE FUNCTION ops.jarvis_job_read_v1(p_job_id text)
RETURNS TABLE (
    id bigint, queue_name varchar, task_name varchar, status ops.procrastinate_job_status,
    args jsonb, attempts integer, progress real, progress_message text, result jsonb,
    error jsonb, created_at timestamptz, started_at timestamptz, finished_at timestamptz
) LANGUAGE sql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
    SELECT job.id, job.queue_name, job.task_name, job.status, job.args, job.attempts,
           progress.progress, progress.message, progress.result, progress.error,
           (SELECT min(event.at) FROM ops.procrastinate_events AS event WHERE event.job_id = job.id),
           (SELECT min(event.at) FROM ops.procrastinate_events AS event WHERE event.job_id = job.id AND event.type = 'started'),
           (SELECT max(event.at) FROM ops.procrastinate_events AS event WHERE event.job_id = job.id AND event.type IN ('succeeded', 'failed', 'cancelled', 'aborted'))
    FROM ops.procrastinate_jobs AS job
    LEFT JOIN ops.job_progress AS progress ON progress.jarvis_job_id = job.args->>'job_id'
    WHERE job.args->>'job_id' = p_job_id
    ORDER BY job.id DESC LIMIT 1
$$;

CREATE OR REPLACE FUNCTION ops.jarvis_job_list_v1(
    p_status text, p_kind text, p_user_id text, p_limit integer
) RETURNS TABLE (
    id text, kind varchar, user_id text, status text, payload jsonb, result jsonb,
    error jsonb, progress double precision, progress_message text, created_at timestamptz,
    started_at timestamptz, finished_at timestamptz, source text
) LANGUAGE sql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
    SELECT job.args->>'job_id', job.task_name, job.args->>'user_id',
           CASE job.status WHEN 'todo' THEN 'queued' WHEN 'doing' THEN 'running'
               WHEN 'aborting' THEN 'running' WHEN 'aborted' THEN 'cancelled'
               ELSE job.status::text END,
           job.args - 'job_id' - 'user_id', progress.result, progress.error,
           COALESCE(progress.progress, 0)::double precision, progress.message,
           (SELECT min(event.at) FROM ops.procrastinate_events AS event WHERE event.job_id = job.id),
           (SELECT min(event.at) FROM ops.procrastinate_events AS event WHERE event.job_id = job.id AND event.type = 'started'),
           (SELECT max(event.at) FROM ops.procrastinate_events AS event WHERE event.job_id = job.id AND event.type IN ('succeeded', 'failed', 'cancelled', 'aborted')),
           'procrastinate'
    FROM ops.procrastinate_jobs AS job
    LEFT JOIN ops.job_progress AS progress ON progress.jarvis_job_id = job.args->>'job_id'
    WHERE job.args ? 'job_id'
      AND (p_kind IS NULL OR job.task_name = p_kind)
      AND ((p_user_id IS NULL AND job.args->>'user_id' IS NULL) OR job.args->>'user_id' = p_user_id)
      AND (p_status IS NULL OR (p_status = 'active' AND job.status IN ('todo', 'doing', 'aborting'))
           OR (p_status = 'queued' AND job.status = 'todo')
           OR (p_status = 'running' AND job.status IN ('doing', 'aborting'))
           OR (p_status = 'cancelled' AND job.status IN ('cancelled', 'aborted'))
           OR p_status = job.status::text)
    ORDER BY job.id DESC LIMIT LEAST(GREATEST(p_limit, 1), 500)
$$;

CREATE OR REPLACE FUNCTION ops.jarvis_job_cancel_v1(p_job_id text, p_user_id text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
DECLARE v_id bigint;
BEGIN
    SELECT id INTO v_id FROM ops.procrastinate_jobs
    WHERE args->>'job_id' = p_job_id AND args->>'user_id' = p_user_id
    ORDER BY id DESC LIMIT 1 FOR UPDATE;
    IF v_id IS NULL THEN
        RETURN FALSE;
    END IF;
    UPDATE ops.procrastinate_jobs
    SET abort_requested = true,
        status = CASE status WHEN 'todo' THEN 'cancelled'::ops.procrastinate_job_status ELSE status END
    WHERE id = v_id AND status IN ('todo', 'doing');
    RETURN TRUE;
END;
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA ops FROM jarvis_platform_runtime;
REVOKE ALL ON FUNCTION ops.jarvis_job_read_v1(text), ops.jarvis_job_list_v1(text, text, text, integer),
    ops.jarvis_job_cancel_v1(text, text) FROM PUBLIC;
GRANT USAGE ON SCHEMA ops TO jarvis_platform_runtime;
GRANT EXECUTE ON FUNCTION ops.jarvis_job_read_v1(text), ops.jarvis_job_list_v1(text, text, text, integer),
    ops.jarvis_job_cancel_v1(text, text)
    TO jarvis_platform_runtime;
GRANT SELECT ON ops.jarvis_jobs_rollback_v1 TO jarvis_legacy_rollback;
RESET ROLE;
