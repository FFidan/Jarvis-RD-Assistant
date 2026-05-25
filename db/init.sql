-- =============================================================================
-- JARVIS RD Assistant - PostgreSQL Schema
-- =============================================================================
-- This file is mounted into the postgres container at
-- /docker-entrypoint-initdb.d/01_init.sql and runs on first database creation.
--
-- Conventions:
--   - All tables: snake_case, plural
--   - All timestamps: TIMESTAMPTZ (timezone-aware)
--   - All tables include created_at DEFAULT NOW()
--   - JSONB for flexible/evolving data structures
--   - Foreign keys with ON DELETE CASCADE where parent owns children
--   - This schema reflects the post-migration steady state for fresh installs.
--     Versioned migrations remain the source of truth for upgrades.
-- =============================================================================

-- =============================================================================
-- SHARED HELPERS
-- =============================================================================
-- Defined here (rather than alongside its consuming triggers near the bottom of
-- the file) because triggers from migrations 046+ reference set_updated_at()
-- before the section that originally introduced it (migration 042). The
-- CREATE OR REPLACE block further down is harmless — it idempotently re-applies
-- the same body and then attaches the legacy migration-042 triggers.

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END $$ LANGUAGE plpgsql;


CREATE TYPE public.procrastinate_job_event_type AS ENUM (
    'deferred',
    'started',
    'deferred_for_retry',
    'failed',
    'succeeded',
    'cancelled',
    'abort_requested',
    'aborted',
    'scheduled',
    'retried'
);
CREATE TYPE public.procrastinate_job_status AS ENUM (
    'todo',
    'doing',
    'succeeded',
    'failed',
    'cancelled',
    'aborting',
    'aborted'
);
CREATE TYPE public.procrastinate_job_to_defer_v1 AS (
	queue_name character varying,
	task_name character varying,
	priority integer,
	lock text,
	queueing_lock text,
	args jsonb,
	scheduled_at timestamp with time zone
);
CREATE FUNCTION public.papers_search_vector_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector :=
        to_tsvector('english', coalesce(NEW.title, '')) ||
        to_tsvector('english', coalesce(NEW.abstract, '')) ||
        to_tsvector('english', coalesce(array_to_string(NEW.authors, ' '), ''));
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.procrastinate_cancel_job_v1(job_id bigint, abort boolean, delete_job boolean) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
BEGIN
    IF delete_job THEN
        DELETE FROM procrastinate_jobs
        WHERE id = job_id AND status = 'todo'
        RETURNING id INTO _job_id;
    END IF;
    IF _job_id IS NULL THEN
        IF abort THEN
            UPDATE procrastinate_jobs
            SET abort_requested = true,
                status = CASE status
                    WHEN 'todo' THEN 'cancelled'::procrastinate_job_status ELSE status
                END
            WHERE id = job_id AND status IN ('todo', 'doing')
            RETURNING id INTO _job_id;
        ELSE
            UPDATE procrastinate_jobs
            SET status = 'cancelled'::procrastinate_job_status
            WHERE id = job_id AND status = 'todo'
            RETURNING id INTO _job_id;
        END IF;
    END IF;
    RETURN _job_id;
END;
$$;
CREATE FUNCTION public.procrastinate_defer_jobs_v1(jobs public.procrastinate_job_to_defer_v1[]) RETURNS bigint[]
    LANGUAGE plpgsql
    AS $$
DECLARE
    job_ids bigint[];
BEGIN
    WITH inserted_jobs AS (
        INSERT INTO procrastinate_jobs (queue_name, task_name, priority, lock, queueing_lock, args, scheduled_at)
        SELECT (job).queue_name,
               (job).task_name,
               (job).priority,
               (job).lock,
               (job).queueing_lock,
               (job).args,
               (job).scheduled_at
        FROM unnest(jobs) AS job
        RETURNING id
    )
    SELECT array_agg(id) FROM inserted_jobs INTO job_ids;

    RETURN job_ids;
END;
$$;
CREATE FUNCTION public.procrastinate_defer_periodic_job_v2(_queue_name character varying, _lock character varying, _queueing_lock character varying, _task_name character varying, _priority integer, _periodic_id character varying, _defer_timestamp bigint, _args jsonb) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
DECLARE
	_job_id bigint;
	_defer_id bigint;
BEGIN
    INSERT
        INTO procrastinate_periodic_defers (task_name, periodic_id, defer_timestamp)
        VALUES (_task_name, _periodic_id, _defer_timestamp)
        ON CONFLICT DO NOTHING
        RETURNING id into _defer_id;

    IF _defer_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE procrastinate_periodic_defers
        SET job_id = (
            SELECT COALESCE((
                SELECT unnest(procrastinate_defer_jobs_v1(
                    ARRAY[
                        ROW(
                            _queue_name,
                            _task_name,
                            _priority,
                            _lock,
                            _queueing_lock,
                            _args,
                            NULL::timestamptz
                        )
                    ]::procrastinate_job_to_defer_v1[]
                ))
            ), NULL)
        )
        WHERE id = _defer_id
        RETURNING job_id INTO _job_id;

    DELETE
        FROM procrastinate_periodic_defers
        USING (
            SELECT id
            FROM procrastinate_periodic_defers
            WHERE procrastinate_periodic_defers.task_name = _task_name
            AND procrastinate_periodic_defers.periodic_id = _periodic_id
            AND procrastinate_periodic_defers.defer_timestamp < _defer_timestamp
            ORDER BY id
            FOR UPDATE
        ) to_delete
        WHERE procrastinate_periodic_defers.id = to_delete.id;

    RETURN _job_id;
END;
$$;
CREATE TABLE public.procrastinate_jobs (
    id bigint NOT NULL,
    queue_name character varying(128) NOT NULL,
    task_name character varying(128) NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    lock text,
    queueing_lock text,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    status public.procrastinate_job_status DEFAULT 'todo'::public.procrastinate_job_status NOT NULL,
    scheduled_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    abort_requested boolean DEFAULT false NOT NULL,
    worker_id bigint,
    CONSTRAINT check_not_todo_abort_requested CHECK ((NOT ((status = 'todo'::public.procrastinate_job_status) AND (abort_requested = true))))
);
CREATE FUNCTION public.procrastinate_fetch_job_v2(target_queue_names character varying[], p_worker_id bigint) RETURNS public.procrastinate_jobs
    LANGUAGE plpgsql
    AS $$
DECLARE
	found_jobs procrastinate_jobs;
BEGIN
    WITH candidate AS (
        SELECT jobs.*
            FROM procrastinate_jobs AS jobs
            WHERE
                -- reject the job if its lock has earlier or higher priority jobs
                NOT EXISTS (
                    SELECT 1
                        FROM procrastinate_jobs AS other_jobs
                        WHERE
                            jobs.lock IS NOT NULL
                            AND other_jobs.lock = jobs.lock
                            AND (
                                -- job with same lock is already running
                                other_jobs.status = 'doing'
                                OR
                                -- job with same lock is waiting and has higher priority (or same priority but was queued first)
                                (
                                    other_jobs.status = 'todo'
                                    AND (
                                        other_jobs.priority > jobs.priority
                                        OR (
                                        other_jobs.priority = jobs.priority
                                        AND other_jobs.id < jobs.id
                                        )
                                    )
                                )
                            )
                )
                AND jobs.status = 'todo'
                AND (target_queue_names IS NULL OR jobs.queue_name = ANY( target_queue_names ))
                AND (jobs.scheduled_at IS NULL OR jobs.scheduled_at <= now())
            ORDER BY jobs.priority DESC, jobs.id ASC LIMIT 1
            FOR UPDATE OF jobs SKIP LOCKED
    )
    UPDATE procrastinate_jobs
        SET status = 'doing', worker_id = p_worker_id
        FROM candidate
        WHERE procrastinate_jobs.id = candidate.id
        RETURNING procrastinate_jobs.* INTO found_jobs;

 RETURN found_jobs;
END;
$$;
CREATE FUNCTION public.procrastinate_finish_job_v1(job_id bigint, end_status public.procrastinate_job_status, delete_job boolean) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
BEGIN
    IF end_status NOT IN ('succeeded', 'failed', 'aborted') THEN
        RAISE 'End status should be either "succeeded", "failed" or "aborted" (job id: %)', job_id;
    END IF;
    IF delete_job THEN
        DELETE FROM procrastinate_jobs
        WHERE id = job_id AND status IN ('todo', 'doing')
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = end_status,
            abort_requested = false,
            attempts = CASE status
                WHEN 'doing' THEN attempts + 1 ELSE attempts
            END
        WHERE id = job_id AND status IN ('todo', 'doing')
        RETURNING id INTO _job_id;
    END IF;
    IF _job_id IS NULL THEN
        RAISE 'Job was not found or not in "doing" or "todo" status (job id: %)', job_id;
    END IF;
END;
$$;
CREATE FUNCTION public.procrastinate_notify_queue_abort_job_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    payload TEXT;
BEGIN
    SELECT json_build_object('type', 'abort_job_requested', 'job_id', NEW.id)::text INTO payload;
	PERFORM pg_notify('procrastinate_queue_v1#' || NEW.queue_name, payload);
	PERFORM pg_notify('procrastinate_any_queue_v1', payload);
	RETURN NEW;
END;
$$;
CREATE FUNCTION public.procrastinate_notify_queue_job_inserted_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    payload TEXT;
BEGIN
    SELECT json_build_object('type', 'job_inserted', 'job_id', NEW.id)::text INTO payload;
	PERFORM pg_notify('procrastinate_queue_v1#' || NEW.queue_name, payload);
	PERFORM pg_notify('procrastinate_any_queue_v1', payload);
	RETURN NEW;
END;
$$;
CREATE FUNCTION public.procrastinate_prune_stalled_workers_v1(seconds_since_heartbeat double precision) RETURNS TABLE(worker_id bigint)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    DELETE FROM procrastinate_workers
    WHERE last_heartbeat < NOW() - (seconds_since_heartbeat || 'SECOND')::INTERVAL
    RETURNING procrastinate_workers.id;
END;
$$;
CREATE FUNCTION public.procrastinate_register_worker_v1() RETURNS TABLE(worker_id bigint)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    INSERT INTO procrastinate_workers DEFAULT VALUES
    RETURNING procrastinate_workers.id;
END;
$$;
CREATE FUNCTION public.procrastinate_retry_job_v1(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
    _abort_requested boolean;
BEGIN
    SELECT abort_requested FROM procrastinate_jobs
    WHERE id = job_id AND status = 'doing'
    FOR UPDATE
    INTO _abort_requested;
    IF _abort_requested THEN
        UPDATE procrastinate_jobs
        SET status = 'failed'::procrastinate_job_status
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = 'todo'::procrastinate_job_status,
            attempts = attempts + 1,
            scheduled_at = retry_at,
            priority = COALESCE(new_priority, priority),
            queue_name = COALESCE(new_queue_name, queue_name),
            lock = COALESCE(new_lock, lock)
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    END IF;

    IF _job_id IS NULL THEN
        RAISE 'Job was not found or not in "doing" status (job id: %)', job_id;
    END IF;
END;
$$;
CREATE FUNCTION public.procrastinate_retry_job_v2(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
    _abort_requested boolean;
    _current_status procrastinate_job_status;
BEGIN
    SELECT status, abort_requested FROM procrastinate_jobs
    WHERE id = job_id AND status IN ('doing', 'failed')
    FOR UPDATE
    INTO _current_status, _abort_requested;
    IF _current_status = 'doing' AND _abort_requested THEN
        UPDATE procrastinate_jobs
        SET status = 'failed'::procrastinate_job_status
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = 'todo'::procrastinate_job_status,
            attempts = attempts + 1,
            scheduled_at = retry_at,
            priority = COALESCE(new_priority, priority),
            queue_name = COALESCE(new_queue_name, queue_name),
            lock = COALESCE(new_lock, lock)
        WHERE id = job_id AND status IN ('doing', 'failed')
        RETURNING id INTO _job_id;
    END IF;

    IF _job_id IS NULL THEN
        RAISE 'Job was not found or has an invalid status to retry (job id: %)', job_id;
    END IF;

END;
$$;
CREATE FUNCTION public.procrastinate_trigger_abort_requested_events_procedure_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type)
        VALUES (NEW.id, 'abort_requested'::procrastinate_job_event_type);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.procrastinate_trigger_function_scheduled_events_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type, at)
        VALUES (NEW.id, 'scheduled'::procrastinate_job_event_type, NEW.scheduled_at);

	RETURN NEW;
END;
$$;
CREATE FUNCTION public.procrastinate_trigger_function_status_events_insert_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type)
        VALUES (NEW.id, 'deferred'::procrastinate_job_event_type);
	RETURN NEW;
END;
$$;
CREATE FUNCTION public.procrastinate_trigger_function_status_events_update_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    WITH t AS (
        SELECT CASE
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND NEW.status = 'doing'::procrastinate_job_status
                THEN 'started'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'todo'::procrastinate_job_status
                THEN 'deferred_for_retry'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'failed'::procrastinate_job_status
                THEN 'failed'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'succeeded'::procrastinate_job_status
                THEN 'succeeded'::procrastinate_job_event_type
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND (
                    NEW.status = 'cancelled'::procrastinate_job_status
                    OR NEW.status = 'failed'::procrastinate_job_status
                    OR NEW.status = 'succeeded'::procrastinate_job_status
                )
                THEN 'cancelled'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'aborted'::procrastinate_job_status
                THEN 'aborted'::procrastinate_job_event_type
            WHEN OLD.status = 'failed'::procrastinate_job_status
                AND NEW.status = 'todo'::procrastinate_job_status
                THEN 'retried'::procrastinate_job_event_type
            ELSE NULL
        END as event_type
    )
    INSERT INTO procrastinate_events(job_id, type)
        SELECT NEW.id, t.event_type
        FROM t
        WHERE t.event_type IS NOT NULL;
	RETURN NEW;
END;
$$;
CREATE FUNCTION public.procrastinate_unlink_periodic_defers_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE procrastinate_periodic_defers
    SET job_id = NULL
    WHERE job_id = OLD.id;
    RETURN OLD;
END;
$$;
CREATE FUNCTION public.procrastinate_unregister_worker_v1(worker_id bigint) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    DELETE FROM procrastinate_workers
    WHERE id = worker_id;
END;
$$;
CREATE FUNCTION public.procrastinate_update_heartbeat_v1(worker_id bigint) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE procrastinate_workers
    SET last_heartbeat = NOW()
    WHERE id = worker_id;
END;
$$;

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    user_id text,
    action text NOT NULL,
    resource text NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);
CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.author_alert_log (
    id integer NOT NULL,
    tracked_author_id integer,
    paper_id integer,
    notified_at timestamp with time zone DEFAULT now(),
    user_id integer
);
COMMENT ON TABLE public.author_alert_log IS 'Deduplication log for author alert notifications.';
CREATE SEQUENCE public.author_alert_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.cards (
    id integer NOT NULL,
    deck_id integer,
    paper_id integer,
    card_type character varying(20) NOT NULL,
    front text NOT NULL,
    back text NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    fsrs_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    due_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    user_id integer,
    CONSTRAINT cards_card_type_check CHECK ((card_type IN ('concept', 'quote', 'method', 'comparison')))
);
COMMENT ON TABLE public.cards IS 'Spaced repetition flashcards with FSRS scheduling state.';
COMMENT ON COLUMN public.cards.card_type IS 'One of: concept, quote, method, comparison.';
COMMENT ON COLUMN public.cards.evidence IS '{quote, page_number, chunk_id, pdf_snapshot_path}.';
COMMENT ON COLUMN public.cards.fsrs_state IS 'Serialized py-fsrs Card: stability, difficulty, reps, lapses, state, due.';
COMMENT ON COLUMN public.cards.due_at IS 'Denormalized from fsrs_state.due for efficient indexed queries.';
CREATE SEQUENCE public.cards_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.daily_intent (
    user_id integer,
    intent_date date NOT NULL,
    intent_text text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.daily_log (
    id integer NOT NULL,
    user_id integer,
    log_date date NOT NULL,
    tasks_completed integer DEFAULT 0,
    cards_reviewed integer DEFAULT 0,
    papers_read integer DEFAULT 0,
    focus_hours double precision DEFAULT 0,
    notes text,
    created_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.daily_log IS 'Daily activity summary for analytics and streaks.';
CREATE SEQUENCE public.daily_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.decks (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    topic_id integer,
    created_at timestamp with time zone DEFAULT now(),
    user_id integer
);
COMMENT ON TABLE public.decks IS 'Flashcard decks, optionally linked to a research topic.';
CREATE SEQUENCE public.decks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.entities (
    id integer NOT NULL,
    name text NOT NULL,
    canonical_name text NOT NULL,
    entity_type character varying(50) NOT NULL,
    description text,
    metadata jsonb DEFAULT '{}'::jsonb,
    embedding_id character varying(255),
    paper_count integer DEFAULT 1,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT entities_entity_type_check CHECK ((entity_type IN ('method', 'dataset', 'metric', 'author', 'institution', 'concept')))
);
COMMENT ON TABLE public.entities IS 'Knowledge graph entities extracted from papers via LLM.';
CREATE SEQUENCE public.entities_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.entity_relationships (
    id integer NOT NULL,
    source_entity_id integer NOT NULL,
    target_entity_id integer NOT NULL,
    relationship_type character varying(100) NOT NULL,
    paper_id integer,
    evidence_quote text,
    confidence double precision DEFAULT 1.0,
    page_number integer,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.entity_relationships IS 'Relationships between knowledge graph entities with evidence.';
CREATE SEQUENCE public.entity_relationships_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.extraction_templates (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    fields jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_default boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.extraction_templates IS 'User-defined templates for extracting structured fields from papers.';
CREATE SEQUENCE public.extraction_templates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.job_progress (
    jarvis_job_id text NOT NULL,
    progress real DEFAULT 0 NOT NULL,
    message text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    result jsonb,
    error jsonb
);
CREATE TABLE public.journal_entries (
    id integer NOT NULL,
    user_id integer,
    date date DEFAULT CURRENT_DATE NOT NULL,
    prompts jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.journal_entries_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.llm_usage_log (
    id integer NOT NULL,
    provider character varying(100),
    workflow character varying(100),
    prompt_tokens integer,
    completion_tokens integer,
    cost_usd numeric(10,6),
    created_at timestamp with time zone DEFAULT now(),
    user_id integer
);
COMMENT ON TABLE public.llm_usage_log IS 'Tracks LLM token usage and costs per workflow for analytics.';
CREATE SEQUENCE public.llm_usage_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE IF NOT EXISTS public.magic_link_tokens (
    token_hash text NOT NULL,
    user_id bigint NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    pending_email text
);
CREATE TABLE public.milestones (
    id integer NOT NULL,
    project_id integer,
    name character varying(255) NOT NULL,
    deadline timestamp with time zone NOT NULL,
    description text,
    completed boolean DEFAULT false,
    completed_at timestamp with time zone,
    user_id integer,
    created_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.milestones IS 'Project milestones with deadlines for Telegram reminder nudges.';
CREATE SEQUENCE public.milestones_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_chunks (
    id integer NOT NULL,
    paper_id integer,
    chunk_index integer NOT NULL,
    content text NOT NULL,
    page_number integer,
    start_char integer,
    end_char integer,
    embedding_id character varying(255),
    embedding_model character varying(100),
    created_at timestamp with time zone DEFAULT now(),
    user_id integer
);
COMMENT ON TABLE public.paper_chunks IS 'PDF text split into chunks for RAG. Each chunk maps to a Qdrant vector.';
CREATE SEQUENCE public.paper_chunks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_citations (
    id integer NOT NULL,
    source_paper_id integer NOT NULL,
    cited_paper_id integer NOT NULL,
    citation_context text,
    is_influential boolean,
    intent text[],
    fetched_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.paper_citations IS 'Citation relationships between papers fetched from Semantic Scholar.';
CREATE SEQUENCE public.paper_citations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_contradictions (
    id integer NOT NULL,
    paper_a_id integer NOT NULL,
    paper_b_id integer NOT NULL,
    finding_a text NOT NULL,
    finding_b text NOT NULL,
    quote_a text NOT NULL,
    quote_b text NOT NULL,
    page_a integer,
    page_b integer,
    contradiction_type character varying(50) DEFAULT 'direct'::character varying NOT NULL,
    explanation text NOT NULL,
    confidence double precision NOT NULL,
    status character varying(20) DEFAULT 'verified'::character varying NOT NULL,
    scanner_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id integer,
    CONSTRAINT chk_paper_contradictions_distinct_papers CHECK ((paper_a_id <> paper_b_id)),
    CONSTRAINT paper_contradictions_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT paper_contradictions_contradiction_type_check CHECK ((contradiction_type IN ('direct', 'methodological', 'result', 'interpretation'))),
    CONSTRAINT paper_contradictions_status_check CHECK ((status IN ('verified', 'dismissed', 'false_positive')))
);
COMMENT ON TABLE public.paper_contradictions IS 'Verified cross-paper contradictions. Both quotes must pass QuoteVerifier before insert.';
COMMENT ON COLUMN public.paper_contradictions.scanner_metadata IS 'Scanner version, candidate score, model, and other non-authoritative diagnostics.';
CREATE SEQUENCE public.paper_contradictions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_entities (
    paper_id integer NOT NULL,
    entity_id integer NOT NULL,
    mention_count integer DEFAULT 1,
    first_chunk_id integer,
    user_id integer
);
COMMENT ON TABLE public.paper_entities IS 'Many-to-many link between papers and extracted entities.';
CREATE TABLE public.paper_extractions (
    id integer NOT NULL,
    paper_id integer NOT NULL,
    template_id integer NOT NULL,
    extractions jsonb DEFAULT '{}'::jsonb NOT NULL,
    extraction_model character varying(100),
    extraction_raw text,
    created_at timestamp with time zone DEFAULT now(),
    user_id integer
);
COMMENT ON TABLE public.paper_extractions IS 'LLM-extracted structured data from papers using templates.';
CREATE SEQUENCE public.paper_extractions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_notes (
    id integer NOT NULL,
    paper_id integer NOT NULL,
    user_note text NOT NULL,
    highlight_text text,
    page_number integer,
    source text DEFAULT 'user'::text NOT NULL,
    zotero_annotation_key text,
    verification_status text DEFAULT 'unverified'::text NOT NULL,
    verified_quote text,
    verified_page_number integer,
    promoted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    user_id integer,
    CONSTRAINT paper_notes_source_check CHECK ((source = ANY (ARRAY['user'::text, 'zotero'::text]))),
    CONSTRAINT paper_notes_verification_status_check CHECK ((verification_status = ANY (ARRAY['unverified'::text, 'verified'::text, 'failed'::text])))
);
COMMENT ON TABLE public.paper_notes IS 'User annotations on papers, optionally linked to a page or highlighted text.';
CREATE SEQUENCE public.paper_notes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_recommendations (
    id integer NOT NULL,
    paper_id integer NOT NULL,
    score double precision NOT NULL,
    modes text[] DEFAULT '{}'::text[] NOT NULL,
    explanation text DEFAULT ''::text NOT NULL,
    dismissed boolean DEFAULT false NOT NULL,
    clicked boolean DEFAULT false NOT NULL,
    recommended_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id integer
);
CREATE SEQUENCE public.paper_recommendations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_sources (
    id integer NOT NULL,
    source_type character varying(50) NOT NULL,
    enabled boolean DEFAULT true,
    priority integer DEFAULT 1 NOT NULL,
    config jsonb DEFAULT '{}'::jsonb,
    display_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.paper_sources IS 'Pluggable paper source registry. Each row is a configured source.';
CREATE SEQUENCE public.paper_sources_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_summaries (
    id integer NOT NULL,
    paper_id integer,
    summary_brief text NOT NULL,
    summary_detailed text NOT NULL,
    tldr text,
    key_findings jsonb DEFAULT '[]'::jsonb NOT NULL,
    methodology text,
    limitations text,
    relevance_notes text,
    confidence character varying(10) DEFAULT 'HIGH'::character varying,
    cross_references jsonb DEFAULT '[]'::jsonb,
    llm_model character varying(100),
    llm_prompt text,
    llm_raw_response text,
    summary_verified boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    user_id integer,
    CONSTRAINT paper_summaries_confidence_check CHECK ((confidence IN ('HIGH', 'MEDIUM', 'LOW')))
);
COMMENT ON TABLE public.paper_summaries IS 'LLM-generated summaries with verified citations. See key_findings JSONB.';
COMMENT ON COLUMN public.paper_summaries.key_findings IS 'Array of {finding, quote, page_number, chunk_id}. Quotes verified against source.';
COMMENT ON COLUMN public.paper_summaries.confidence IS 'HIGH, MEDIUM, or LOW based on quote verification pass rate.';
COMMENT ON COLUMN public.paper_summaries.cross_references IS 'Array of {related_paper_id, relationship, explanation, related_quote}.';
COMMENT ON COLUMN public.paper_summaries.llm_prompt IS 'The exact prompt sent to the LLM (audit trail).';
COMMENT ON COLUMN public.paper_summaries.llm_raw_response IS 'The raw LLM response before parsing (audit trail).';
COMMENT ON COLUMN public.paper_summaries.summary_verified IS 'TRUE only when confidence=HIGH. Summary text is LLM prose, not independently verified against source.';
CREATE SEQUENCE public.paper_summaries_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.paper_topics (
    paper_id integer NOT NULL,
    topic_id integer NOT NULL,
    relevance_score double precision
);
COMMENT ON TABLE public.paper_topics IS 'Many-to-many link between papers and topics with LLM-scored relevance.';
CREATE TABLE public.paper_user_state (
    id integer NOT NULL,
    paper_id integer,
    state text DEFAULT 'inbox'::text NOT NULL,
    state_before_trash text,
    starred boolean DEFAULT false NOT NULL,
    user_notes text,
    rating smallint,
    flagged boolean DEFAULT false,
    notified_at timestamp with time zone,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id integer,
    CONSTRAINT paper_user_state_rating_check CHECK (((rating >= 1) AND (rating <= 5))),
    CONSTRAINT paper_user_state_state_before_trash_check CHECK (((state_before_trash IS NULL) OR (state_before_trash = ANY (ARRAY['inbox'::text, 'to_read'::text, 'reading'::text, 'done'::text])))),
    CONSTRAINT paper_user_state_state_check CHECK ((state = ANY (ARRAY['inbox'::text, 'to_read'::text, 'reading'::text, 'done'::text, 'trash'::text])))
);
COMMENT ON TABLE public.paper_user_state IS 'Per-paper user state: lifecycle position (state), star (curation flag), reading metadata. Recommendation feedback lives in a separate table (recommendation_feedback).';
COMMENT ON COLUMN public.paper_user_state.state IS 'Lifecycle position: inbox (untriaged), to_read (saved), reading (engaging), done (finished), trash (rejected). Replaces 5 booleans + status enum from migration 046.';
COMMENT ON COLUMN public.paper_user_state.state_before_trash IS 'For trash rows: the state to restore to. NULL for non-trash rows.';
COMMENT ON COLUMN public.paper_user_state.starred IS 'Per-user favourite flag, orthogonal to state. Triggers zotero.push when project-linked.';
COMMENT ON COLUMN public.paper_user_state.flagged IS 'User flagged this summary as potentially inaccurate.';
CREATE SEQUENCE public.paper_user_state_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.papers (
    id integer NOT NULL,
    external_id character varying(255) NOT NULL,
    source_type character varying(50) NOT NULL,
    title text NOT NULL,
    authors text[] NOT NULL,
    abstract text,
    published_date date,
    url text NOT NULL,
    pdf_url text,
    pdf_local_path text,
    pdf_downloaded boolean DEFAULT false,
    citation_count integer DEFAULT 0,
    priority_score double precision,
    metadata jsonb DEFAULT '{}'::jsonb,
    discovered_at timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now(),
    citations_fetched_at timestamp with time zone,
    search_vector tsvector,
    discovered_by integer,
    discovery_origin text DEFAULT 'user_initiated'::text NOT NULL,
    CONSTRAINT papers_discovery_origin_check CHECK ((discovery_origin = ANY (ARRAY['user_initiated'::text, 'pulse'::text, 'recommender'::text, 'citation_batch'::text])))
);
COMMENT ON TABLE public.papers IS 'All ingested papers. Metadata comes from source APIs, never from LLMs.';
COMMENT ON COLUMN public.papers.discovered_by IS 'Audit only: which user (or NULL for system) first discovered this paper. Library membership lives in user_library, not here. Sprint B (migration 072).';
COMMENT ON COLUMN public.papers.discovery_origin IS 'How the paper first entered the system. Immutable. Values: user_initiated (manual search/upload/Zotero/citation graph), pulse (overnight discovery), recommender (paper_recommendations), citation_batch (citation graph batch save).';
CREATE SEQUENCE public.papers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.procrastinate_events (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    type public.procrastinate_job_event_type,
    at timestamp with time zone DEFAULT now()
);
CREATE SEQUENCE public.procrastinate_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE SEQUENCE public.procrastinate_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.procrastinate_periodic_defers (
    id bigint NOT NULL,
    task_name character varying(128) NOT NULL,
    defer_timestamp bigint,
    job_id bigint,
    periodic_id character varying(128) DEFAULT ''::character varying NOT NULL
);
CREATE SEQUENCE public.procrastinate_periodic_defers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.procrastinate_workers (
    id bigint NOT NULL,
    last_heartbeat timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE public.procrastinate_workers ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.procrastinate_workers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.project_papers (
    project_id integer NOT NULL,
    paper_id integer NOT NULL,
    notes text,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);
COMMENT ON TABLE public.project_papers IS 'Many-to-many link between projects and papers with optional notes.';
CREATE TABLE public.project_questions (
    id integer NOT NULL,
    project_id integer NOT NULL,
    user_id integer NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
COMMENT ON TABLE public.project_questions IS 'Per-project open research questions (Projects document-pane § OPEN QUESTIONS).';
CREATE SEQUENCE public.project_questions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.projects (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    status character varying(20) DEFAULT 'active'::character varying,
    deadline timestamp with time zone,
    color character varying(7),
    user_id integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    next_milestone text,
    next_milestone_due date,
    CONSTRAINT projects_color_check CHECK (((color IS NULL) OR ((color)::text ~ '^#[0-9A-Fa-f]{6}$'::text))),
    CONSTRAINT projects_status_check CHECK ((status IN ('active', 'paused', 'completed', 'archived')))
);
COMMENT ON TABLE public.projects IS 'Research projects with deadlines.';
COMMENT ON COLUMN public.projects.status IS 'One of: active, paused, completed, archived.';
CREATE SEQUENCE public.projects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.pulse_cards (
    id integer NOT NULL,
    deck_id integer NOT NULL,
    paper_id integer NOT NULL,
    rank integer NOT NULL,
    score double precision NOT NULL,
    llm_relevance integer,
    llm_novelty integer,
    reasoning text,
    signals jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    user_id integer,
    reasoning_verified boolean,
    reasoning_confidence character varying(10) DEFAULT NULL::character varying,
    CONSTRAINT pulse_cards_reasoning_confidence_check CHECK (((reasoning_confidence IS NULL) OR (reasoning_confidence IN ('HIGH', 'MEDIUM', 'LOW', 'UNVERIFIED'))))
);
CREATE SEQUENCE public.pulse_cards_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.pulse_decks (
    id integer NOT NULL,
    deck_date date NOT NULL,
    card_count integer DEFAULT 0 NOT NULL,
    generated_at timestamp with time zone DEFAULT now(),
    stats jsonb DEFAULT '{}'::jsonb,
    degraded_reason text,
    user_id integer,
    is_stale boolean DEFAULT false NOT NULL,
    stale_from_deck_id integer
);
CREATE SEQUENCE public.pulse_decks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.pulse_models (
    id integer NOT NULL,
    user_id integer,
    model_version text DEFAULT 'v1'::text NOT NULL,
    model_blob bytea NOT NULL,
    feature_names jsonb DEFAULT '[]'::jsonb NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    trained_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.pulse_models_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.recommendation_feedback (
    id bigint NOT NULL,
    paper_id bigint NOT NULL,
    user_id integer,
    signal text NOT NULL,
    source text NOT NULL,
    topic_id bigint,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recommendation_feedback_signal_check CHECK ((signal = ANY (ARRAY['positive'::text, 'negative'::text]))),
    CONSTRAINT recommendation_feedback_source_check CHECK ((source = ANY (ARRAY['pulse_thumbs'::text, 'feed_thumbs'::text, 'paper_detail_thumbs'::text, 'dismiss_combined'::text])))
);
COMMENT ON TABLE public.recommendation_feedback IS 'Single source of truth for recommendation-quality user signals. Replaces pulse_ratings (dropped in migration 049).';
CREATE SEQUENCE public.recommendation_feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.review_logs (
    id integer NOT NULL,
    card_id integer,
    rating smallint NOT NULL,
    review_duration_ms integer,
    reviewed_at timestamp with time zone DEFAULT now(),
    fsrs_log jsonb DEFAULT '{}'::jsonb NOT NULL,
    user_id integer,
    idempotency_key text,
    CONSTRAINT review_logs_rating_check CHECK (((rating >= 1) AND (rating <= 4)))
);
COMMENT ON TABLE public.review_logs IS 'History of flashcard reviews. Rating: 1=Again, 2=Hard, 3=Good, 4=Easy.';
CREATE SEQUENCE public.review_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.scheduled_nudges (
    id integer NOT NULL,
    nudge_type character varying(50) NOT NULL,
    cron_expression character varying(100) NOT NULL,
    enabled boolean DEFAULT true,
    config jsonb DEFAULT '{}'::jsonb,
    last_fired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT scheduled_nudges_nudge_type_check CHECK ((nudge_type IN ('deadline_warning', 'daily_summary', 'review_reminder', 'paper_digest', 'research_pulse', 'author_alert')))
);
COMMENT ON TABLE public.scheduled_nudges IS 'Configurable notification schedules for Telegram nudges.';
COMMENT ON COLUMN public.scheduled_nudges.nudge_type IS 'One of: deadline_warning, daily_summary, review_reminder, paper_digest, research_pulse, author_alert.';
CREATE SEQUENCE public.scheduled_nudges_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

CREATE TABLE IF NOT EXISTS public.sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id bigint NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone
);
CREATE TABLE public.source_health (
    id bigint NOT NULL,
    user_id integer,
    source_type text NOT NULL,
    last_request_at timestamp with time zone,
    last_success_at timestamp with time zone,
    last_status text,
    cooldown_until timestamp with time zone,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.source_health_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.source_run_history (
    id bigint NOT NULL,
    user_id integer,
    source_type text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    candidate_count integer DEFAULT 0 NOT NULL,
    duration_ms integer,
    detail jsonb
);
CREATE SEQUENCE public.source_run_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.system_events (
    id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    level text NOT NULL,
    category text NOT NULL,
    source text NOT NULL,
    message text NOT NULL,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    correlation_id uuid,
    CONSTRAINT system_events_category_check CHECK ((category = ANY (ARRAY['error'::text, 'job'::text, 'source'::text, 'auth'::text, 'config'::text, 'infra'::text]))),
    CONSTRAINT system_events_level_check CHECK ((level = ANY (ARRAY['debug'::text, 'info'::text, 'warning'::text, 'error'::text, 'critical'::text])))
);
CREATE SEQUENCE public.system_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.task_paper_links (
    task_id integer NOT NULL,
    paper_id integer NOT NULL,
    note text
);
COMMENT ON TABLE public.task_paper_links IS 'Links papers to tasks for project-scoped reading lists.';
CREATE TABLE public.tasks (
    id integer NOT NULL,
    project_id integer,
    parent_task_id integer,
    title character varying(500) NOT NULL,
    description text,
    status character varying(20) DEFAULT 'todo'::character varying,
    priority smallint DEFAULT 3,
    deadline timestamp with time zone,
    estimated_hours double precision,
    actual_hours double precision,
    sort_order integer DEFAULT 0,
    user_id integer,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    color text,
    CONSTRAINT tasks_priority_check CHECK (((priority >= 1) AND (priority <= 4))),
    CONSTRAINT tasks_status_check CHECK ((status IN ('todo', 'in_progress', 'blocked', 'done')))
);
COMMENT ON TABLE public.tasks IS 'Tasks within projects. Supports subtasks via parent_task_id.';
COMMENT ON COLUMN public.tasks.status IS 'One of: todo, in_progress, blocked, done.';
COMMENT ON COLUMN public.tasks.priority IS '1=critical, 2=high, 3=medium, 4=low.';
CREATE SEQUENCE public.tasks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.telegram_pairing (
    code text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);
CREATE TABLE public.telegram_pairing_tokens (
    token text NOT NULL,
    user_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone
);
CREATE TABLE public.telegram_user_pairings (
    user_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    telegram_username text,
    paired_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.thread (
    id integer NOT NULL,
    user_id integer,
    title text NOT NULL,
    anchor text,
    progress real DEFAULT 0 NOT NULL,
    last_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thread_progress_check CHECK (((progress >= (0)::double precision) AND (progress <= (1)::double precision))),
    CONSTRAINT thread_status_check CHECK ((status = ANY (ARRAY['open'::text, 'done'::text, 'archived'::text])))
);
CREATE SEQUENCE public.thread_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.topics (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    query_terms text[] NOT NULL,
    category character varying(100),
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    description text
);
COMMENT ON TABLE public.topics IS 'User-defined research topics with search query terms.';
COMMENT ON COLUMN public.topics.description IS 'Optional free-text context used by the Pulse scoring LLM. Null = fall back to name + query_terms.';
CREATE SEQUENCE public.topics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.tracked_authors (
    id integer NOT NULL,
    author_name text NOT NULL,
    s2_author_id character varying(50),
    source character varying(20) DEFAULT 'manual'::character varying NOT NULL,
    enabled boolean DEFAULT true,
    last_checked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    user_id integer,
    CONSTRAINT tracked_authors_source_check CHECK ((source IN ('manual', 'auto_starred', 'auto_rated')))
);
COMMENT ON TABLE public.tracked_authors IS 'Authors to track for new-publication alerts.';
CREATE SEQUENCE public.tracked_authors_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.user_config (
    id integer NOT NULL,
    user_id integer,
    key character varying(255) NOT NULL,
    value jsonb,
    encrypted_value bytea,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.user_config IS 'Key-value store for system defaults and per-user preferences.';
CREATE SEQUENCE public.user_config_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE TABLE public.user_library (
    user_id integer NOT NULL,
    paper_id integer NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    added_via text NOT NULL,
    CONSTRAINT user_library_added_via_check CHECK ((added_via = ANY (ARRAY['manual_save'::text, 'batch_save'::text, 'zotero_pull'::text, 'pulse_acceptance'::text, 'auto_fetch_topic_match'::text, 'backfill_engagement'::text, 'backfill_legacy_user_id'::text, 'topic_discovery'::text, 'citation_graph'::text])))
);
COMMENT ON TABLE public.user_library IS 'Per-user library entries (Sprint B canonical-corpus refactor). Each row represents "user U has paper P in their library". Replaces the muddled `papers.user_id IS NULL OR papers.user_id = $N` predicate.';
CREATE TABLE public.user_topic_subscriptions (
    user_id integer NOT NULL,
    topic_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS public.users (
    id bigint NOT NULL,
    email text NOT NULL,
    role text DEFAULT 'user'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone,
    deleted_at timestamp with time zone,
    display_name text,
    CONSTRAINT users_role_check CHECK ((role = ANY (ARRAY['user'::text, 'admin'::text])))
);
CREATE SEQUENCE public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;
ALTER SEQUENCE public.author_alert_log_id_seq OWNED BY public.author_alert_log.id;
ALTER SEQUENCE public.cards_id_seq OWNED BY public.cards.id;
ALTER SEQUENCE public.daily_log_id_seq OWNED BY public.daily_log.id;
ALTER SEQUENCE public.decks_id_seq OWNED BY public.decks.id;
ALTER SEQUENCE public.entities_id_seq OWNED BY public.entities.id;
ALTER SEQUENCE public.entity_relationships_id_seq OWNED BY public.entity_relationships.id;
ALTER SEQUENCE public.extraction_templates_id_seq OWNED BY public.extraction_templates.id;
ALTER SEQUENCE public.journal_entries_id_seq OWNED BY public.journal_entries.id;
ALTER SEQUENCE public.llm_usage_log_id_seq OWNED BY public.llm_usage_log.id;
ALTER SEQUENCE public.milestones_id_seq OWNED BY public.milestones.id;
ALTER SEQUENCE public.paper_chunks_id_seq OWNED BY public.paper_chunks.id;
ALTER SEQUENCE public.paper_citations_id_seq OWNED BY public.paper_citations.id;
ALTER SEQUENCE public.paper_contradictions_id_seq OWNED BY public.paper_contradictions.id;
ALTER SEQUENCE public.paper_extractions_id_seq OWNED BY public.paper_extractions.id;
ALTER SEQUENCE public.paper_notes_id_seq OWNED BY public.paper_notes.id;
ALTER SEQUENCE public.paper_recommendations_id_seq OWNED BY public.paper_recommendations.id;
ALTER SEQUENCE public.paper_sources_id_seq OWNED BY public.paper_sources.id;
ALTER SEQUENCE public.paper_summaries_id_seq OWNED BY public.paper_summaries.id;
ALTER SEQUENCE public.paper_user_state_id_seq OWNED BY public.paper_user_state.id;
ALTER SEQUENCE public.papers_id_seq OWNED BY public.papers.id;
ALTER SEQUENCE public.procrastinate_events_id_seq OWNED BY public.procrastinate_events.id;
ALTER SEQUENCE public.procrastinate_jobs_id_seq OWNED BY public.procrastinate_jobs.id;
ALTER SEQUENCE public.procrastinate_periodic_defers_id_seq OWNED BY public.procrastinate_periodic_defers.id;
ALTER SEQUENCE public.project_questions_id_seq OWNED BY public.project_questions.id;
ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;
ALTER SEQUENCE public.pulse_cards_id_seq OWNED BY public.pulse_cards.id;
ALTER SEQUENCE public.pulse_decks_id_seq OWNED BY public.pulse_decks.id;
ALTER SEQUENCE public.pulse_models_id_seq OWNED BY public.pulse_models.id;
ALTER SEQUENCE public.recommendation_feedback_id_seq OWNED BY public.recommendation_feedback.id;
ALTER SEQUENCE public.review_logs_id_seq OWNED BY public.review_logs.id;
ALTER SEQUENCE public.scheduled_nudges_id_seq OWNED BY public.scheduled_nudges.id;
ALTER SEQUENCE public.source_health_id_seq OWNED BY public.source_health.id;
ALTER SEQUENCE public.source_run_history_id_seq OWNED BY public.source_run_history.id;
ALTER SEQUENCE public.system_events_id_seq OWNED BY public.system_events.id;
ALTER SEQUENCE public.tasks_id_seq OWNED BY public.tasks.id;
ALTER SEQUENCE public.thread_id_seq OWNED BY public.thread.id;
ALTER SEQUENCE public.topics_id_seq OWNED BY public.topics.id;
ALTER SEQUENCE public.tracked_authors_id_seq OWNED BY public.tracked_authors.id;
ALTER SEQUENCE public.user_config_id_seq OWNED BY public.user_config.id;
ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;
ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);
ALTER TABLE ONLY public.author_alert_log ALTER COLUMN id SET DEFAULT nextval('public.author_alert_log_id_seq'::regclass);
ALTER TABLE ONLY public.cards ALTER COLUMN id SET DEFAULT nextval('public.cards_id_seq'::regclass);
ALTER TABLE ONLY public.daily_log ALTER COLUMN id SET DEFAULT nextval('public.daily_log_id_seq'::regclass);
ALTER TABLE ONLY public.decks ALTER COLUMN id SET DEFAULT nextval('public.decks_id_seq'::regclass);
ALTER TABLE ONLY public.entities ALTER COLUMN id SET DEFAULT nextval('public.entities_id_seq'::regclass);
ALTER TABLE ONLY public.entity_relationships ALTER COLUMN id SET DEFAULT nextval('public.entity_relationships_id_seq'::regclass);
ALTER TABLE ONLY public.extraction_templates ALTER COLUMN id SET DEFAULT nextval('public.extraction_templates_id_seq'::regclass);
ALTER TABLE ONLY public.journal_entries ALTER COLUMN id SET DEFAULT nextval('public.journal_entries_id_seq'::regclass);
ALTER TABLE ONLY public.llm_usage_log ALTER COLUMN id SET DEFAULT nextval('public.llm_usage_log_id_seq'::regclass);
ALTER TABLE ONLY public.milestones ALTER COLUMN id SET DEFAULT nextval('public.milestones_id_seq'::regclass);
ALTER TABLE ONLY public.paper_chunks ALTER COLUMN id SET DEFAULT nextval('public.paper_chunks_id_seq'::regclass);
ALTER TABLE ONLY public.paper_citations ALTER COLUMN id SET DEFAULT nextval('public.paper_citations_id_seq'::regclass);
ALTER TABLE ONLY public.paper_contradictions ALTER COLUMN id SET DEFAULT nextval('public.paper_contradictions_id_seq'::regclass);
ALTER TABLE ONLY public.paper_extractions ALTER COLUMN id SET DEFAULT nextval('public.paper_extractions_id_seq'::regclass);
ALTER TABLE ONLY public.paper_notes ALTER COLUMN id SET DEFAULT nextval('public.paper_notes_id_seq'::regclass);
ALTER TABLE ONLY public.paper_recommendations ALTER COLUMN id SET DEFAULT nextval('public.paper_recommendations_id_seq'::regclass);
ALTER TABLE ONLY public.paper_sources ALTER COLUMN id SET DEFAULT nextval('public.paper_sources_id_seq'::regclass);
ALTER TABLE ONLY public.paper_summaries ALTER COLUMN id SET DEFAULT nextval('public.paper_summaries_id_seq'::regclass);
ALTER TABLE ONLY public.paper_user_state ALTER COLUMN id SET DEFAULT nextval('public.paper_user_state_id_seq'::regclass);
ALTER TABLE ONLY public.papers ALTER COLUMN id SET DEFAULT nextval('public.papers_id_seq'::regclass);
ALTER TABLE ONLY public.procrastinate_events ALTER COLUMN id SET DEFAULT nextval('public.procrastinate_events_id_seq'::regclass);
ALTER TABLE ONLY public.procrastinate_jobs ALTER COLUMN id SET DEFAULT nextval('public.procrastinate_jobs_id_seq'::regclass);
ALTER TABLE ONLY public.procrastinate_periodic_defers ALTER COLUMN id SET DEFAULT nextval('public.procrastinate_periodic_defers_id_seq'::regclass);
ALTER TABLE ONLY public.project_questions ALTER COLUMN id SET DEFAULT nextval('public.project_questions_id_seq'::regclass);
ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);
ALTER TABLE ONLY public.pulse_cards ALTER COLUMN id SET DEFAULT nextval('public.pulse_cards_id_seq'::regclass);
ALTER TABLE ONLY public.pulse_decks ALTER COLUMN id SET DEFAULT nextval('public.pulse_decks_id_seq'::regclass);
ALTER TABLE ONLY public.pulse_models ALTER COLUMN id SET DEFAULT nextval('public.pulse_models_id_seq'::regclass);
ALTER TABLE ONLY public.recommendation_feedback ALTER COLUMN id SET DEFAULT nextval('public.recommendation_feedback_id_seq'::regclass);
ALTER TABLE ONLY public.review_logs ALTER COLUMN id SET DEFAULT nextval('public.review_logs_id_seq'::regclass);
ALTER TABLE ONLY public.scheduled_nudges ALTER COLUMN id SET DEFAULT nextval('public.scheduled_nudges_id_seq'::regclass);
ALTER TABLE ONLY public.source_health ALTER COLUMN id SET DEFAULT nextval('public.source_health_id_seq'::regclass);
ALTER TABLE ONLY public.source_run_history ALTER COLUMN id SET DEFAULT nextval('public.source_run_history_id_seq'::regclass);
ALTER TABLE ONLY public.system_events ALTER COLUMN id SET DEFAULT nextval('public.system_events_id_seq'::regclass);
ALTER TABLE ONLY public.tasks ALTER COLUMN id SET DEFAULT nextval('public.tasks_id_seq'::regclass);
ALTER TABLE ONLY public.thread ALTER COLUMN id SET DEFAULT nextval('public.thread_id_seq'::regclass);
ALTER TABLE ONLY public.topics ALTER COLUMN id SET DEFAULT nextval('public.topics_id_seq'::regclass);
ALTER TABLE ONLY public.tracked_authors ALTER COLUMN id SET DEFAULT nextval('public.tracked_authors_id_seq'::regclass);
ALTER TABLE ONLY public.user_config ALTER COLUMN id SET DEFAULT nextval('public.user_config_id_seq'::regclass);
ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.author_alert_log
    ADD CONSTRAINT author_alert_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.daily_log
    ADD CONSTRAINT daily_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.daily_log
    ADD CONSTRAINT daily_log_user_id_log_date_key UNIQUE NULLS NOT DISTINCT (user_id, log_date);
ALTER TABLE ONLY public.decks
    ADD CONSTRAINT decks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_canonical_name_entity_type_key UNIQUE (canonical_name, entity_type);
ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_source_entity_id_target_entity_id_rela_key UNIQUE (source_entity_id, target_entity_id, relationship_type, paper_id);
ALTER TABLE ONLY public.extraction_templates
    ADD CONSTRAINT extraction_templates_name_key UNIQUE (name);
ALTER TABLE ONLY public.extraction_templates
    ADD CONSTRAINT extraction_templates_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.job_progress
    ADD CONSTRAINT job_progress_pkey PRIMARY KEY (jarvis_job_id);
ALTER TABLE ONLY public.journal_entries
    ADD CONSTRAINT journal_entries_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.journal_entries
    ADD CONSTRAINT journal_entries_user_id_date_key UNIQUE NULLS NOT DISTINCT (user_id, date);
ALTER TABLE ONLY public.llm_usage_log
    ADD CONSTRAINT llm_usage_log_pkey PRIMARY KEY (id);
DO $$ BEGIN
ALTER TABLE ONLY public.magic_link_tokens
    ADD CONSTRAINT magic_link_tokens_pkey PRIMARY KEY (token_hash);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_chunks
    ADD CONSTRAINT paper_chunks_paper_id_chunk_index_key UNIQUE (paper_id, chunk_index);
ALTER TABLE ONLY public.paper_chunks
    ADD CONSTRAINT paper_chunks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_citations
    ADD CONSTRAINT paper_citations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_citations
    ADD CONSTRAINT paper_citations_source_paper_id_cited_paper_id_key UNIQUE (source_paper_id, cited_paper_id);
ALTER TABLE ONLY public.paper_contradictions
    ADD CONSTRAINT paper_contradictions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_entities
    ADD CONSTRAINT paper_entities_pkey PRIMARY KEY (paper_id, entity_id);
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_paper_id_template_id_key UNIQUE (paper_id, template_id);
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_notes
    ADD CONSTRAINT paper_notes_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_recommendations
    ADD CONSTRAINT paper_recommendations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_sources
    ADD CONSTRAINT paper_sources_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_sources
    ADD CONSTRAINT paper_sources_source_type_key UNIQUE (source_type);
ALTER TABLE ONLY public.paper_summaries
    ADD CONSTRAINT paper_summaries_paper_id_user_id_key UNIQUE NULLS NOT DISTINCT (paper_id, user_id);
ALTER TABLE ONLY public.paper_summaries
    ADD CONSTRAINT paper_summaries_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_topics
    ADD CONSTRAINT paper_topics_pkey PRIMARY KEY (paper_id, topic_id);
ALTER TABLE ONLY public.paper_user_state
    ADD CONSTRAINT paper_user_state_paper_id_user_id_key UNIQUE NULLS NOT DISTINCT (paper_id, user_id);
ALTER TABLE ONLY public.paper_user_state
    ADD CONSTRAINT paper_user_state_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.papers
    ADD CONSTRAINT papers_external_id_key UNIQUE (external_id);
ALTER TABLE ONLY public.papers
    ADD CONSTRAINT papers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.procrastinate_events
    ADD CONSTRAINT procrastinate_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.procrastinate_jobs
    ADD CONSTRAINT procrastinate_jobs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_unique UNIQUE (task_name, periodic_id, defer_timestamp);
ALTER TABLE ONLY public.procrastinate_workers
    ADD CONSTRAINT procrastinate_workers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.project_papers
    ADD CONSTRAINT project_papers_pkey PRIMARY KEY (project_id, paper_id);
ALTER TABLE ONLY public.project_questions
    ADD CONSTRAINT project_questions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pulse_cards
    ADD CONSTRAINT pulse_cards_deck_id_paper_id_key UNIQUE (deck_id, paper_id);
ALTER TABLE ONLY public.pulse_cards
    ADD CONSTRAINT pulse_cards_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pulse_decks
    ADD CONSTRAINT pulse_decks_deck_date_user_id_key UNIQUE NULLS NOT DISTINCT (deck_date, user_id);
ALTER TABLE ONLY public.pulse_decks
    ADD CONSTRAINT pulse_decks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pulse_models
    ADD CONSTRAINT pulse_models_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.recommendation_feedback
    ADD CONSTRAINT recommendation_feedback_paper_user_source_uniq UNIQUE NULLS NOT DISTINCT (paper_id, user_id, source);
ALTER TABLE ONLY public.recommendation_feedback
    ADD CONSTRAINT recommendation_feedback_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.review_logs
    ADD CONSTRAINT review_logs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.scheduled_nudges
    ADD CONSTRAINT scheduled_nudges_nudge_type_key UNIQUE (nudge_type);
ALTER TABLE ONLY public.scheduled_nudges
    ADD CONSTRAINT scheduled_nudges_pkey PRIMARY KEY (id);

DO $$ BEGIN
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE ONLY public.source_health
    ADD CONSTRAINT source_health_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.source_health
    ADD CONSTRAINT source_health_user_source UNIQUE NULLS NOT DISTINCT (user_id, source_type);
ALTER TABLE ONLY public.source_run_history
    ADD CONSTRAINT source_run_history_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.system_events
    ADD CONSTRAINT system_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_paper_links
    ADD CONSTRAINT task_paper_links_pkey PRIMARY KEY (task_id, paper_id);
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.telegram_pairing
    ADD CONSTRAINT telegram_pairing_pkey PRIMARY KEY (code);
ALTER TABLE ONLY public.telegram_pairing_tokens
    ADD CONSTRAINT telegram_pairing_tokens_pkey PRIMARY KEY (token);
ALTER TABLE ONLY public.telegram_user_pairings
    ADD CONSTRAINT telegram_user_pairings_chat_id_key UNIQUE (chat_id);
ALTER TABLE ONLY public.telegram_user_pairings
    ADD CONSTRAINT telegram_user_pairings_pkey PRIMARY KEY (user_id);
ALTER TABLE ONLY public.thread
    ADD CONSTRAINT thread_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tracked_authors
    ADD CONSTRAINT tracked_authors_name_s2_unique UNIQUE NULLS NOT DISTINCT (author_name, s2_author_id);
ALTER TABLE ONLY public.tracked_authors
    ADD CONSTRAINT tracked_authors_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.paper_recommendations
    ADD CONSTRAINT uq_paper_recommendations_paper_user_id UNIQUE NULLS NOT DISTINCT (paper_id, user_id);
ALTER TABLE ONLY public.user_config
    ADD CONSTRAINT user_config_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.user_library
    ADD CONSTRAINT user_library_pkey PRIMARY KEY (user_id, paper_id);
ALTER TABLE ONLY public.user_topic_subscriptions
    ADD CONSTRAINT user_topic_subscriptions_pkey PRIMARY KEY (user_id, topic_id);
DO $$ BEGIN
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE UNIQUE INDEX daily_intent_user_date_uniq ON public.daily_intent USING btree (user_id, intent_date) NULLS NOT DISTINCT;
CREATE INDEX idx_audit_log_action ON public.audit_log USING btree (action, "timestamp" DESC);
CREATE INDEX idx_audit_log_action_created ON public.audit_log USING btree (action, "timestamp" DESC);
CREATE INDEX idx_audit_log_timestamp ON public.audit_log USING btree ("timestamp" DESC);
CREATE INDEX idx_audit_log_user ON public.audit_log USING btree (user_id, "timestamp" DESC);
CREATE OR REPLACE RULE no_delete_audit_log AS ON DELETE TO public.audit_log DO INSTEAD NOTHING;
CREATE OR REPLACE RULE no_update_audit_log AS ON UPDATE TO public.audit_log DO INSTEAD NOTHING;
CREATE INDEX idx_author_alert_log_user ON public.author_alert_log USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE UNIQUE INDEX author_alert_log_dedupe ON public.author_alert_log USING btree (tracked_author_id, paper_id, user_id);
CREATE INDEX idx_cards_deck ON public.cards USING btree (deck_id);
CREATE INDEX idx_cards_due ON public.cards USING btree (due_at) WHERE (due_at IS NOT NULL);
CREATE INDEX idx_cards_paper ON public.cards USING btree (paper_id);
CREATE INDEX idx_cards_user ON public.cards USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_citations_cited ON public.paper_citations USING btree (cited_paper_id);
CREATE INDEX idx_citations_source ON public.paper_citations USING btree (source_paper_id);
CREATE INDEX idx_decks_user ON public.decks USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_entities_canonical ON public.entities USING btree (canonical_name);
CREATE INDEX idx_entities_type ON public.entities USING btree (entity_type);
CREATE INDEX idx_entity_rels_paper ON public.entity_relationships USING btree (paper_id);
CREATE INDEX idx_entity_rels_source ON public.entity_relationships USING btree (source_entity_id);
CREATE INDEX idx_entity_rels_target ON public.entity_relationships USING btree (target_entity_id);
CREATE INDEX idx_llm_usage_created ON public.llm_usage_log USING btree (created_at);
CREATE INDEX idx_llm_usage_log_user_id ON public.llm_usage_log USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_magic_link_tokens_user_expires ON public.magic_link_tokens USING btree (user_id, expires_at);
CREATE INDEX idx_milestones_project ON public.milestones USING btree (project_id);
CREATE INDEX idx_milestones_user ON public.milestones USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_chunks_paper ON public.paper_chunks USING btree (paper_id);
CREATE INDEX idx_paper_chunks_user ON public.paper_chunks USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_contradictions_paper_a ON public.paper_contradictions USING btree (paper_a_id, status, created_at DESC);
CREATE INDEX idx_paper_contradictions_paper_b ON public.paper_contradictions USING btree (paper_b_id, status, created_at DESC);
CREATE INDEX idx_paper_contradictions_status ON public.paper_contradictions USING btree (status, created_at DESC);
CREATE UNIQUE INDEX idx_paper_contradictions_unique_quotes ON public.paper_contradictions USING btree (LEAST(paper_a_id, paper_b_id), GREATEST(paper_a_id, paper_b_id), md5(quote_a), md5(quote_b));
CREATE INDEX idx_paper_contradictions_user ON public.paper_contradictions USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_entities_entity ON public.paper_entities USING btree (entity_id);
CREATE INDEX idx_paper_extractions_paper ON public.paper_extractions USING btree (paper_id);
CREATE INDEX idx_paper_extractions_template ON public.paper_extractions USING btree (template_id);
CREATE INDEX idx_paper_extractions_user ON public.paper_extractions USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_notes_paper ON public.paper_notes USING btree (paper_id);
CREATE INDEX idx_paper_notes_paper_source ON public.paper_notes USING btree (paper_id, source, created_at DESC);
CREATE INDEX idx_paper_notes_search ON public.paper_notes USING gin (to_tsvector('english'::regconfig, ((COALESCE(user_note, ''::text) || ' '::text) || COALESCE(highlight_text, ''::text))));
CREATE INDEX idx_paper_notes_user ON public.paper_notes USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_recommendations_score ON public.paper_recommendations USING btree (score DESC);
CREATE INDEX idx_paper_recommendations_user ON public.paper_recommendations USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_recommendations_user_score_active ON public.paper_recommendations USING btree (user_id, score DESC) WHERE (NOT dismissed);
CREATE INDEX idx_paper_summaries_user ON public.paper_summaries USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_topics_topic ON public.paper_topics USING btree (topic_id);
CREATE INDEX idx_paper_user_state_state ON public.paper_user_state USING btree (state);
CREATE INDEX idx_paper_user_state_user ON public.paper_user_state USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_papers_created ON public.papers USING btree (created_at);
CREATE INDEX idx_papers_discovered_by ON public.papers USING btree (discovered_by) WHERE (discovered_by IS NOT NULL);
CREATE INDEX idx_papers_discovery_origin ON public.papers USING btree (discovery_origin);
CREATE INDEX idx_papers_external_id_normalized ON public.papers USING btree (lower(btrim((external_id)::text)));
CREATE INDEX idx_papers_metadata_arxiv_id ON public.papers USING btree (lower(btrim((metadata ->> 'arxiv_id'::text)))) WHERE (metadata ? 'arxiv_id'::text);
CREATE INDEX idx_papers_metadata_doi ON public.papers USING btree (lower(btrim((metadata ->> 'doi'::text)))) WHERE (metadata ? 'doi'::text);
CREATE INDEX idx_papers_priority ON public.papers USING btree (priority_score DESC NULLS LAST);
CREATE INDEX idx_papers_search_vector ON public.papers USING gin (search_vector);
CREATE INDEX idx_papers_source_type ON public.papers USING btree (source_type);
CREATE INDEX idx_papers_title_year_normalized ON public.papers USING btree (regexp_replace(lower(btrim(title)), '[^[:alnum:]_[:space:]]'::text, ' '::text, 'g'::text), EXTRACT(year FROM published_date)) WHERE ((title IS NOT NULL) AND (published_date IS NOT NULL));
CREATE INDEX idx_procrastinate_jobs_worker_not_null ON public.procrastinate_jobs USING btree (worker_id) WHERE ((worker_id IS NOT NULL) AND (status = 'doing'::public.procrastinate_job_status));
CREATE INDEX idx_procrastinate_workers_last_heartbeat ON public.procrastinate_workers USING btree (last_heartbeat);
CREATE INDEX idx_project_papers_paper ON public.project_papers USING btree (paper_id);
CREATE INDEX idx_project_papers_project ON public.project_papers USING btree (project_id);
CREATE INDEX idx_project_questions_project ON public.project_questions USING btree (project_id);
CREATE INDEX idx_project_questions_user ON public.project_questions USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_projects_user ON public.projects USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_pulse_cards_deck_rank ON public.pulse_cards USING btree (deck_id, rank);
CREATE INDEX idx_pulse_cards_user ON public.pulse_cards USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_pulse_decks_user ON public.pulse_decks USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_pulse_models_trained_at ON public.pulse_models USING btree (trained_at DESC);
CREATE INDEX idx_pulse_models_user_id ON public.pulse_models USING btree (user_id);
CREATE INDEX idx_review_logs_card ON public.review_logs USING btree (card_id);
CREATE INDEX idx_review_logs_user ON public.review_logs USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_user_expires ON public.sessions USING btree (user_id, expires_at) WHERE (revoked_at IS NULL);
CREATE INDEX idx_task_paper_links_paper ON public.task_paper_links USING btree (paper_id);
CREATE INDEX idx_task_paper_links_task ON public.task_paper_links USING btree (task_id);
CREATE INDEX idx_tasks_project ON public.tasks USING btree (project_id);
CREATE INDEX idx_tasks_user ON public.tasks USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_telegram_pairing_tokens_user ON public.telegram_pairing_tokens USING btree (user_id);
CREATE INDEX idx_telegram_user_pairings_chat ON public.telegram_user_pairings USING btree (chat_id);
CREATE INDEX idx_thread_user ON public.thread USING btree (user_id, last_at DESC) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_tracked_authors_enabled ON public.tracked_authors USING btree (enabled) WHERE (enabled = true);
CREATE INDEX idx_tracked_authors_name_null_s2 ON public.tracked_authors USING btree (author_name) WHERE (s2_author_id IS NULL);
CREATE INDEX idx_tracked_authors_user ON public.tracked_authors USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_user_config_user_id ON public.user_config USING btree (user_id);
CREATE INDEX idx_user_library_paper ON public.user_library USING btree (paper_id);
CREATE INDEX idx_user_library_user_added ON public.user_library USING btree (user_id, added_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_email_active ON public.users USING btree (email) WHERE (deleted_at IS NULL);
CREATE INDEX idx_uts_topic ON public.user_topic_subscriptions USING btree (topic_id);
CREATE INDEX idx_uts_user ON public.user_topic_subscriptions USING btree (user_id);
CREATE INDEX ix_job_progress_updated_at ON public.job_progress USING btree (updated_at);
CREATE INDEX ix_source_health_lookup ON public.source_health USING btree (user_id, source_type);
CREATE INDEX ix_source_run_history_timeline ON public.source_run_history USING btree (user_id, source_type, started_at DESC);
CREATE INDEX paper_entities_user_id_idx ON public.paper_entities USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX procrastinate_events_job_id_fkey_v1 ON public.procrastinate_events USING btree (job_id);
CREATE INDEX procrastinate_jobs_id_lock_idx_v1 ON public.procrastinate_jobs USING btree (id, lock) WHERE (status = ANY (ARRAY['todo'::public.procrastinate_job_status, 'doing'::public.procrastinate_job_status]));
CREATE UNIQUE INDEX procrastinate_jobs_lock_idx_v1 ON public.procrastinate_jobs USING btree (lock) WHERE (status = 'doing'::public.procrastinate_job_status);
CREATE INDEX procrastinate_jobs_priority_idx_v1 ON public.procrastinate_jobs USING btree (priority DESC, id) WHERE (status = 'todo'::public.procrastinate_job_status);
CREATE INDEX procrastinate_jobs_queue_name_idx_v1 ON public.procrastinate_jobs USING btree (queue_name);
CREATE UNIQUE INDEX procrastinate_jobs_queueing_lock_idx_v1 ON public.procrastinate_jobs USING btree (queueing_lock) WHERE (status = 'todo'::public.procrastinate_job_status);
CREATE INDEX procrastinate_periodic_defers_job_id_fkey_v1 ON public.procrastinate_periodic_defers USING btree (job_id);
CREATE INDEX recommendation_feedback_paper_idx ON public.recommendation_feedback USING btree (paper_id);
CREATE INDEX recommendation_feedback_signal_recent_idx ON public.recommendation_feedback USING btree (signal, created_at DESC);
CREATE INDEX recommendation_feedback_topic_idx ON public.recommendation_feedback USING btree (topic_id) WHERE (topic_id IS NOT NULL);
CREATE INDEX system_events_category_level_idx ON public.system_events USING btree (category, level, created_at DESC);
CREATE INDEX system_events_correlation_idx ON public.system_events USING btree (correlation_id, created_at) WHERE (correlation_id IS NOT NULL);
CREATE INDEX system_events_created_at_idx ON public.system_events USING btree (created_at DESC);
CREATE INDEX telegram_pairing_expires_idx ON public.telegram_pairing USING btree (expires_at);
CREATE UNIQUE INDEX uq_paper_notes_zotero_annotation ON public.paper_notes USING btree (paper_id, zotero_annotation_key) WHERE (zotero_annotation_key IS NOT NULL);
CREATE UNIQUE INDEX uq_pulse_models_one_active_per_user ON public.pulse_models USING btree (COALESCE(user_id, 0)) WHERE (is_active = true);
CREATE UNIQUE INDEX uq_review_logs_user_idempotency ON public.review_logs USING btree (user_id, idempotency_key) WHERE (idempotency_key IS NOT NULL);
CREATE UNIQUE INDEX user_config_user_key_idx ON public.user_config USING btree (user_id, key) NULLS NOT DISTINCT;
CREATE TRIGGER papers_search_vector_trigger BEFORE INSERT OR UPDATE OF title, abstract, authors ON public.papers FOR EACH ROW EXECUTE FUNCTION public.papers_search_vector_update();
CREATE TRIGGER procrastinate_jobs_notify_queue_job_aborted_v1 AFTER UPDATE OF abort_requested ON public.procrastinate_jobs FOR EACH ROW WHEN (((old.abort_requested = false) AND (new.abort_requested = true) AND (new.status = 'doing'::public.procrastinate_job_status))) EXECUTE FUNCTION public.procrastinate_notify_queue_abort_job_v1();
CREATE TRIGGER procrastinate_jobs_notify_queue_job_inserted_v1 AFTER INSERT ON public.procrastinate_jobs FOR EACH ROW WHEN ((new.status = 'todo'::public.procrastinate_job_status)) EXECUTE FUNCTION public.procrastinate_notify_queue_job_inserted_v1();
CREATE TRIGGER procrastinate_trigger_abort_requested_events_v1 AFTER UPDATE OF abort_requested ON public.procrastinate_jobs FOR EACH ROW WHEN ((new.abort_requested = true)) EXECUTE FUNCTION public.procrastinate_trigger_abort_requested_events_procedure_v1();
CREATE TRIGGER procrastinate_trigger_delete_jobs_v1 BEFORE DELETE ON public.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION public.procrastinate_unlink_periodic_defers_v1();
CREATE TRIGGER procrastinate_trigger_scheduled_events_v1 AFTER INSERT OR UPDATE ON public.procrastinate_jobs FOR EACH ROW WHEN (((new.scheduled_at IS NOT NULL) AND (new.status = 'todo'::public.procrastinate_job_status))) EXECUTE FUNCTION public.procrastinate_trigger_function_scheduled_events_v1();
CREATE TRIGGER procrastinate_trigger_status_events_insert_v1 AFTER INSERT ON public.procrastinate_jobs FOR EACH ROW WHEN ((new.status = 'todo'::public.procrastinate_job_status)) EXECUTE FUNCTION public.procrastinate_trigger_function_status_events_insert_v1();
CREATE TRIGGER procrastinate_trigger_status_events_update_v1 AFTER UPDATE OF status ON public.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION public.procrastinate_trigger_function_status_events_update_v1();
CREATE TRIGGER set_updated_at_paper_user_state BEFORE UPDATE ON public.paper_user_state FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE TRIGGER trg_cards_updated_at BEFORE UPDATE ON public.cards FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE TRIGGER trg_extraction_templates_updated_at BEFORE UPDATE ON public.extraction_templates FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE TRIGGER trg_paper_contradictions_updated_at BEFORE UPDATE ON public.paper_contradictions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON public.projects FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE TRIGGER trg_tasks_updated_at BEFORE UPDATE ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
CREATE TRIGGER trg_user_config_updated_at BEFORE UPDATE ON public.user_config FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
ALTER TABLE ONLY public.author_alert_log
    ADD CONSTRAINT author_alert_log_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.author_alert_log
    ADD CONSTRAINT author_alert_log_tracked_author_id_fkey FOREIGN KEY (tracked_author_id) REFERENCES public.tracked_authors(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.author_alert_log
    ADD CONSTRAINT author_alert_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_deck_id_fkey FOREIGN KEY (deck_id) REFERENCES public.decks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.daily_intent
    ADD CONSTRAINT daily_intent_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.daily_log
    ADD CONSTRAINT daily_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.decks
    ADD CONSTRAINT decks_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.decks
    ADD CONSTRAINT decks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_source_entity_id_fkey FOREIGN KEY (source_entity_id) REFERENCES public.entities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_target_entity_id_fkey FOREIGN KEY (target_entity_id) REFERENCES public.entities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.journal_entries
    ADD CONSTRAINT journal_entries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.llm_usage_log
    ADD CONSTRAINT llm_usage_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
DO $$ BEGIN
ALTER TABLE ONLY public.magic_link_tokens
    ADD CONSTRAINT magic_link_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_chunks
    ADD CONSTRAINT paper_chunks_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_chunks
    ADD CONSTRAINT paper_chunks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_citations
    ADD CONSTRAINT paper_citations_cited_paper_id_fkey FOREIGN KEY (cited_paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_citations
    ADD CONSTRAINT paper_citations_source_paper_id_fkey FOREIGN KEY (source_paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_contradictions
    ADD CONSTRAINT paper_contradictions_paper_a_id_fkey FOREIGN KEY (paper_a_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_contradictions
    ADD CONSTRAINT paper_contradictions_paper_b_id_fkey FOREIGN KEY (paper_b_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_contradictions
    ADD CONSTRAINT paper_contradictions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_entities
    ADD CONSTRAINT paper_entities_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_entities
    ADD CONSTRAINT paper_entities_first_chunk_id_fkey FOREIGN KEY (first_chunk_id) REFERENCES public.paper_chunks(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.paper_entities
    ADD CONSTRAINT paper_entities_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_entities
    ADD CONSTRAINT paper_entities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.extraction_templates(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_notes
    ADD CONSTRAINT paper_notes_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_notes
    ADD CONSTRAINT paper_notes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_recommendations
    ADD CONSTRAINT paper_recommendations_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_recommendations
    ADD CONSTRAINT paper_recommendations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_summaries
    ADD CONSTRAINT paper_summaries_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_summaries
    ADD CONSTRAINT paper_summaries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_topics
    ADD CONSTRAINT paper_topics_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_topics
    ADD CONSTRAINT paper_topics_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_user_state
    ADD CONSTRAINT paper_user_state_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_user_state
    ADD CONSTRAINT paper_user_state_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.papers
    ADD CONSTRAINT papers_discovered_by_fkey FOREIGN KEY (discovered_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.procrastinate_events
    ADD CONSTRAINT procrastinate_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.procrastinate_jobs(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.procrastinate_jobs
    ADD CONSTRAINT procrastinate_jobs_worker_id_fkey FOREIGN KEY (worker_id) REFERENCES public.procrastinate_workers(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.procrastinate_jobs(id);
ALTER TABLE ONLY public.project_papers
    ADD CONSTRAINT project_papers_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_papers
    ADD CONSTRAINT project_papers_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_questions
    ADD CONSTRAINT project_questions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_questions
    ADD CONSTRAINT project_questions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.pulse_cards
    ADD CONSTRAINT pulse_cards_deck_id_fkey FOREIGN KEY (deck_id) REFERENCES public.pulse_decks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.pulse_cards
    ADD CONSTRAINT pulse_cards_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.pulse_cards
    ADD CONSTRAINT pulse_cards_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.pulse_decks
    ADD CONSTRAINT pulse_decks_stale_from_deck_id_fkey FOREIGN KEY (stale_from_deck_id) REFERENCES public.pulse_decks(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.pulse_decks
    ADD CONSTRAINT pulse_decks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.pulse_models
    ADD CONSTRAINT pulse_models_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.recommendation_feedback
    ADD CONSTRAINT recommendation_feedback_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.recommendation_feedback
    ADD CONSTRAINT recommendation_feedback_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.recommendation_feedback
    ADD CONSTRAINT recommendation_feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.review_logs
    ADD CONSTRAINT review_logs_card_id_fkey FOREIGN KEY (card_id) REFERENCES public.cards(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.review_logs
    ADD CONSTRAINT review_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
DO $$ BEGIN
ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE ONLY public.source_health
    ADD CONSTRAINT source_health_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.source_run_history
    ADD CONSTRAINT source_run_history_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.task_paper_links
    ADD CONSTRAINT task_paper_links_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.task_paper_links
    ADD CONSTRAINT task_paper_links_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_parent_task_id_fkey FOREIGN KEY (parent_task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.telegram_pairing_tokens
    ADD CONSTRAINT telegram_pairing_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.telegram_user_pairings
    ADD CONSTRAINT telegram_user_pairings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.thread
    ADD CONSTRAINT thread_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.tracked_authors
    ADD CONSTRAINT tracked_authors_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_config
    ADD CONSTRAINT user_config_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_library
    ADD CONSTRAINT user_library_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_library
    ADD CONSTRAINT user_library_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_topic_subscriptions
    ADD CONSTRAINT user_topic_subscriptions_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_topic_subscriptions
    ADD CONSTRAINT user_topic_subscriptions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Seed data (re-homed to end of schema so all constraints/indexes exist)
INSERT INTO user_config (key, value) VALUES
    ('llm.smart_model', '"smart"'),
    ('llm.fast_model', '"fast"'),
    ('llm.embed_model', '"embed"'),
    ('user.timezone', '"UTC"'),
    ('fsrs.desired_retention', '0.9'),
    ('fsrs.learning_steps', '[1, 10]')
ON CONFLICT (user_id, key) DO NOTHING;

INSERT INTO paper_sources (source_type, enabled, config) VALUES
    ('arxiv', TRUE, '{}'),
    ('semantic_scholar', FALSE, '{"key_env": "SEMANTIC_SCHOLAR_API_KEY", "requires_key": false}'),
    ('local', FALSE, '{}')  -- TODO: Enable when local PDF ingestion is implemented
ON CONFLICT (source_type) DO NOTHING;

INSERT INTO scheduled_nudges (nudge_type, cron_expression, enabled) VALUES
    ('daily_summary', '30 8 * * *', TRUE),
    ('paper_digest', '0 9 * * 1', TRUE),
    ('review_reminder', '0 14 * * *', TRUE),
    ('deadline_warning', '0 12 * * *', TRUE),
    ('research_pulse', '0 9 * * *', TRUE),
    ('author_alert', '0 10 * * *', TRUE)
ON CONFLICT (nudge_type) DO NOTHING;

INSERT INTO extraction_templates (name, description, fields, is_default) VALUES
    ('Standard Research Paper', 'Default template for empirical research papers',
     '[{"name":"methodology","label":"Methodology","description":"Research methodology used","type":"text"},
       {"name":"sample_size","label":"Sample Size","description":"Number of participants or samples","type":"number"},
       {"name":"main_finding","label":"Main Finding","description":"Primary result or conclusion","type":"text"},
       {"name":"limitations","label":"Limitations","description":"Acknowledged limitations","type":"text"},
       {"name":"future_work","label":"Future Work","description":"Suggested future directions","type":"text"}]',
     TRUE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO paper_sources (source_type, enabled, config)
VALUES
    ('openalex', FALSE,
     '{"requires_key": true, "key_env": "OPENALEX_API_KEY",
       "homepage": "https://openalex.org",
       "docs": "https://developers.openalex.org/"}'::jsonb),
    ('pubmed', TRUE,
     '{"requires_key": false, "key_env": "PUBMED_API_KEY",
       "homepage": "https://pubmed.ncbi.nlm.nih.gov",
       "docs": "https://www.ncbi.nlm.nih.gov/home/develop/api/"}'::jsonb)
ON CONFLICT (source_type) DO NOTHING;

INSERT INTO user_config (key, value) VALUES
    ('pulse.enabled', 'false'::jsonb),
    ('pulse.cron', '"0 4 * * *"'::jsonb),
    ('pulse.deck_size', '10'::jsonb),
    ('pulse.stage2_top_k', '40'::jsonb),
    ('pulse.weights',
     '{"embedding": 0.2, "topic": 0.2, "llm_relevance": 0.3, "llm_novelty": 0.1, "author_bonus": 0.15, "recency": 0.05, "citation_pagerank": 0.0, "citation_count": 0.0, "citation_adamic_adar": 0.0, "classifier": 0.0}'::jsonb)
ON CONFLICT (user_id, key) DO NOTHING;

INSERT INTO user_config (key, value) VALUES
    ('telegram.owner_chat_id', 'null'::jsonb),
    ('setup.completed',        'false'::jsonb)
ON CONFLICT (user_id, key) DO NOTHING;

-- =============================================================================
-- SCHEMA-MIGRATIONS BOOTSTRAP
-- =============================================================================
-- WAVE 1 MIGRATION SQUASH (2026-05-19): the 88 incremental db/migrations/*.sql
-- files were collapsed into this single regenerated baseline. This file was
-- machine-generated from the real fresh-install path (HEAD db/init.sql +
-- 069_auth.sql + run_migrations() over the full 1..88 chain, then pg_dump
-- --schema-only), so it now embodies EVERY migration 1 through 88.
--
-- POST-PRISTINE AUDIT-REMEDIATION (2026-05-26): migrations 0089/0090/0091
-- were folded directly into this baseline (the repo had never been publicly
-- deployed at that point, so a clean baseline beats fold-forward-keep-as-noop).
-- We therefore pre-mark all 91 versions applied so the runtime runner is a
-- no-op on a fresh install. The runner (libs/jarvis_common/jarvis_common/
-- migrations.py) is KEPT unchanged: the NEXT runtime migration is 0092, which
-- the runner will apply on first boot when db/migrations/0092_*.sql lands.
-- Do not use generate_series (CI-enforced: scripts/check-migrations-no-tx.sh
-- Check 3) -- the explicit contiguous list is the audit trail that init.sql
-- truly embodies each version.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO schema_migrations (version) VALUES
    (1), (2), (3), (4), (5), (6), (7), (8),
    (9), (10), (11), (12), (13), (14), (15), (16),
    (17), (18), (19), (20), (21), (22), (23), (24),
    (25), (26), (27), (28), (29), (30), (31), (32),
    (33), (34), (35), (36), (37), (38), (39), (40),
    (41), (42), (43), (44), (45), (46), (47), (48),
    (49), (50), (51), (52), (53), (54), (55), (56),
    (57), (58), (59), (60), (61), (62), (63), (64),
    (65), (66), (67), (68), (69), (70), (71), (72),
    (73), (74), (75), (76), (77), (78), (79), (80),
    (81), (82), (83), (84), (85), (86), (87), (88),
    (89), (90), (91)
ON CONFLICT (version) DO NOTHING;
