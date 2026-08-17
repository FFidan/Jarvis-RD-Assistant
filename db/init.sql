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
    user_id bigint,
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
    created_at timestamp with time zone DEFAULT now()
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
    stance character varying(10),
    claim_topic text,
    CONSTRAINT chk_paper_contradictions_distinct_papers CHECK ((paper_a_id <> paper_b_id)),
    CONSTRAINT paper_contradictions_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT paper_contradictions_contradiction_type_check CHECK ((contradiction_type IN ('direct', 'methodological', 'result', 'interpretation'))),
    CONSTRAINT paper_contradictions_status_check CHECK ((status IN ('verified', 'dismissed', 'false_positive'))),
    CONSTRAINT paper_contradictions_stance_check CHECK ((stance IS NULL OR stance IN ('supports', 'opposes', 'neutral')))
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
CREATE TABLE public.paper_highlights (
    id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    paper_id integer NOT NULL,
    user_id integer NOT NULL,
    page integer NOT NULL,
    rect jsonb NOT NULL,
    note text,
    color text,
    quote text,
    zotero_annotation_key text,
    created_at timestamp with time zone DEFAULT now()
);
COMMENT ON TABLE public.paper_highlights IS 'Spatial PDF highlights for the in-PDF annotation reader. One row per user highlight on one paper page; rect stores normalized [0,1] top-origin geometry as JSONB. Distinct from paper_notes (free text): highlights carry page geometry so the reader re-anchors them on the rendered PDF.';
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
CREATE TABLE public.paper_user_zotero_links (
    paper_id integer NOT NULL,
    user_id integer NOT NULL,
    zotero_item_key text,
    zotero_citation_key text,
    zotero_attachment_key text,
    zotero_last_pushed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT paper_user_zotero_links_pkey PRIMARY KEY (paper_id, user_id)
);
COMMENT ON TABLE public.paper_user_zotero_links IS 'Per-(paper,user) Zotero linkage. The shared papers row keeps vestigial global zotero_* keys; this table holds the authoritative per-user linkage so each user operates only against their own library keys.';
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
    zotero_item_key text,
    zotero_citation_key text,
    zotero_last_pushed_at timestamp with time zone,
    chunked_at timestamp with time zone,
    zotero_attachment_key text,
    CONSTRAINT papers_discovery_origin_check CHECK ((discovery_origin = ANY (ARRAY['user_initiated'::text, 'pulse'::text, 'recommender'::text, 'citation_batch'::text])))
);
COMMENT ON TABLE public.papers IS 'All ingested papers. Metadata comes from source APIs, never from LLMs.';
COMMENT ON COLUMN public.papers.discovered_by IS 'Audit only: which user (or NULL for system) first discovered this paper. Library membership lives in user_library, not here. (migration 072).';
COMMENT ON COLUMN public.papers.discovery_origin IS 'How the paper first entered the system. Immutable. Values: user_initiated (manual search/upload/Zotero/citation graph), pulse (overnight discovery), recommender (paper_recommendations), citation_batch (citation graph batch save).';
COMMENT ON COLUMN public.papers.zotero_item_key IS 'Zotero item key set by zotero_service.push_paper_to_zotero after a successful push, cleared by force-repush, and used by sync_from_zotero to short-circuit DOI matches. NULL = not yet pushed.';
COMMENT ON COLUMN public.papers.zotero_last_pushed_at IS 'Timestamp of the last successful Zotero push for this paper. Set together with zotero_item_key; never modified by sync_from_zotero (which leaves the original push timestamp intact).';
COMMENT ON COLUMN public.papers.zotero_citation_key IS 'Better BibTeX citation key fetched from the local BBT plugin after a successful Zotero push. NULL if BBT is unavailable or the paper has not been pushed.';
COMMENT ON COLUMN public.papers.zotero_attachment_key IS 'Zotero PDF attachment key (child of zotero_item_key) that exported highlight annotations are parented to. Set at-most-once on first highlight export. NULL = not yet uploaded.';
COMMENT ON COLUMN public.papers.chunked_at IS 'Set when a paper''s full chunk set was successfully embedded and stored. NULL = never completed, or only partially embedded after a mid-paper failure; such papers are re-processed rather than treated as done.';
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
COMMENT ON TABLE public.user_library IS 'Per-user library entries (canonical-corpus refactor). Each row represents "user U has paper P in their library". Replaces the muddled `papers.user_id IS NULL OR papers.user_id = $N` predicate.';
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
    ADD CONSTRAINT paper_entities_paper_entity_user_key UNIQUE NULLS NOT DISTINCT (paper_id, entity_id, user_id);
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_paper_template_user_key UNIQUE NULLS NOT DISTINCT (paper_id, template_id, user_id);
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
    ADD CONSTRAINT tracked_authors_name_s2_unique UNIQUE NULLS NOT DISTINCT (user_id, author_name, s2_author_id);
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
CREATE INDEX idx_paper_contradictions_claim_topic ON public.paper_contradictions USING btree (claim_topic) WHERE (claim_topic IS NOT NULL);
CREATE INDEX idx_paper_contradictions_paper_a ON public.paper_contradictions USING btree (paper_a_id, status, created_at DESC);
CREATE INDEX idx_paper_contradictions_paper_b ON public.paper_contradictions USING btree (paper_b_id, status, created_at DESC);
CREATE INDEX idx_paper_contradictions_status ON public.paper_contradictions USING btree (status, created_at DESC);
CREATE UNIQUE INDEX idx_paper_contradictions_unique_quotes ON public.paper_contradictions USING btree (LEAST(paper_a_id, paper_b_id), GREATEST(paper_a_id, paper_b_id), md5(quote_a), md5(quote_b));
CREATE INDEX idx_paper_contradictions_user ON public.paper_contradictions USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_entities_entity ON public.paper_entities USING btree (entity_id);
CREATE INDEX idx_paper_extractions_paper ON public.paper_extractions USING btree (paper_id);
CREATE INDEX idx_paper_extractions_template ON public.paper_extractions USING btree (template_id);
CREATE INDEX idx_paper_extractions_user ON public.paper_extractions USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE INDEX idx_paper_highlights_paper_user ON public.paper_highlights USING btree (paper_id, user_id);
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
CREATE UNIQUE INDEX uq_paper_highlights_zotero_key ON public.paper_highlights USING btree (user_id, zotero_annotation_key) WHERE (zotero_annotation_key IS NOT NULL);
CREATE UNIQUE INDEX uq_paper_notes_zotero_annotation ON public.paper_notes USING btree (paper_id, user_id, zotero_annotation_key) NULLS NOT DISTINCT WHERE (zotero_annotation_key IS NOT NULL);
CREATE UNIQUE INDEX uq_pu_zotero_item ON public.paper_user_zotero_links USING btree (user_id, zotero_item_key) WHERE (zotero_item_key IS NOT NULL);
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
    ADD CONSTRAINT paper_entities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.extraction_templates(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_extractions
    ADD CONSTRAINT paper_extractions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_highlights
    ADD CONSTRAINT paper_highlights_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_highlights
    ADD CONSTRAINT paper_highlights_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
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
ALTER TABLE ONLY public.paper_user_zotero_links
    ADD CONSTRAINT paper_user_zotero_links_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.paper_user_zotero_links
    ADD CONSTRAINT paper_user_zotero_links_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
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
    ADD CONSTRAINT pulse_models_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
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
-- llm.*_model are seeded at first boot by _autoconfigure_models_hook from the detected hardware tier (not pre-seeded, so autoconfigure's INSERT is not blocked).
INSERT INTO user_config (key, value) VALUES
    ('user.timezone', '"UTC"'),
    ('fsrs.desired_retention', '0.9'),
    ('fsrs.learning_steps', '[1, 10]')
ON CONFLICT (user_id, key) DO NOTHING;

INSERT INTO paper_sources (source_type, enabled, config) VALUES
    ('arxiv', TRUE, '{}'),
    ('semantic_scholar', FALSE, '{"key_env": "SEMANTIC_SCHOLAR_API_KEY", "requires_key": false}'),
    ('local', FALSE, '{}')  -- registry stub: local PDFs are ingested via POST /api/upload-pdf, not through this source's search/fetch path
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
-- MIGRATION SQUASH (2026-05-19): the 88 incremental db/migrations/*.sql
-- files were collapsed into this single regenerated baseline. This file was
-- machine-generated from the real fresh-install path (HEAD db/init.sql +
-- 069_auth.sql + run_migrations() over the full 1..88 chain, then pg_dump
-- --schema-only), so it now embodies EVERY migration 1 through 88.
--
-- 2026-05-26: migrations 0089/0090/0091, 2026-06-14: migrations 0092-0095, and
-- 2026-06-27: migrations 0096-0101 were folded directly into this baseline (the
-- repo had never been publicly deployed at that point, so a clean baseline beats
-- fold-forward-keep-as-noop). We therefore pre-mark all 101 versions applied so
-- the runtime runner is a no-op on a fresh install. The runner (libs/jarvis_common/
-- jarvis_common/migrations.py) is KEPT unchanged: the NEXT runtime migration is
-- 0102, which the runner will apply on first boot when db/migrations/0102_*.sql
-- lands.
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
    (89), (90), (91), (92), (93), (94), (95), (96),
    (97), (98), (99), (100), (101), (102), (103), (104), (105), (106),
    (107), (108), (109), (110), (111), (112), (113), (114), (115), (116), (117)
ON CONFLICT (version) DO NOTHING;

-- The dedicated ``litellm`` admin database is created by the litellm-db-init
-- one-shot in docker-compose.yml (fresh AND existing volumes), never here:
-- CREATE DATABASE cannot run in this file — the test harness applies it via
-- asyncpg in one implicit transaction, and psql meta-commands don't parse.

-- =============================================================================
-- FRESH-INSTALL OWNERSHIP BOUNDARY
-- =============================================================================
-- 0102: WebAuthn/passkey credential storage.
--
-- Stores registered passkeys (webauthn_credentials), short-lived single-use
-- ceremony nonces (webauthn_challenges), and links a browser session to the
-- passkey it was minted from (sessions.credential_id). The migration runner
-- wraps this file in a transaction and strips any outer BEGIN/COMMIT, so none
-- appear here. All statements are idempotent (IF NOT EXISTS) for safe re-apply.
--
-- FK column types match the baseline: users.id and sessions.user_id are bigint;
-- sessions.id (and thus webauthn_credentials.id) is uuid.

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id bytea UNIQUE NOT NULL,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL DEFAULT 0,
    transports text[],
    aaguid uuid,
    nickname text,
    created_at timestamptz DEFAULT now(),
    last_used_at timestamptz
);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    challenge bytea PRIMARY KEY,
    user_id bigint REFERENCES users(id) ON DELETE CASCADE,
    purpose text NOT NULL,
    expires_at timestamptz NOT NULL
);

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS credential_id uuid
        REFERENCES webauthn_credentials(id) ON DELETE SET NULL;
-- Purge stale group/supergroup telegram pairings (chat_id < 0) created before the
-- private-chat-only pairing guard; they still receive outbound scheduled pushes.
DELETE FROM telegram_user_pairings WHERE chat_id < 0;
-- 0104: paper_chunks are canonical paper data, not user-owned records.
-- A user deletion must never cascade into chunks retained by another library.
ALTER TABLE IF EXISTS paper_chunks
    DROP CONSTRAINT IF EXISTS paper_chunks_user_id_fkey;

DROP INDEX IF EXISTS idx_paper_chunks_user;

ALTER TABLE IF EXISTS paper_chunks
    DROP COLUMN IF EXISTS user_id;
-- 0105: Bridge upgraded deployments to the explicit instance-owner model.
-- Only one unambiguous live administrator may be selected automatically.
WITH live_admin AS MATERIALIZED (
    SELECT id
    FROM users
    WHERE role = 'admin' AND deleted_at IS NULL
), inserted_owner AS (
    INSERT INTO user_config (user_id, key, value)
    SELECT NULL, 'owner.user_id', to_jsonb(live_admin.id)
    FROM live_admin
    WHERE (SELECT COUNT(*) FROM live_admin) = 1
      AND NOT EXISTS (
          SELECT 1
          FROM user_config
          WHERE user_id IS NULL AND key = 'owner.user_id'
      )
    RETURNING value
)
INSERT INTO audit_log (user_id, action, resource, metadata)
SELECT
    value #>> '{}',
    'owner.backfilled',
    'owner.user_id',
    jsonb_build_object('source', 'migration_0105', 'owner_user_id', value)
FROM inserted_owner;
-- 0106: Persist the source-aware paper visibility boundary.
--
-- Provenance labels are descriptive. Public visibility is backfilled only for
-- known scholarly adapters whose row did not enter through the client-driven
-- citation-batch path. Every other existing and future row defaults private.
ALTER TABLE papers
    ADD COLUMN IF NOT EXISTS visibility_scope text NOT NULL DEFAULT 'private';

DO $$ BEGIN
    ALTER TABLE papers
        ADD CONSTRAINT papers_visibility_scope_check
        CHECK (visibility_scope IN ('public', 'private'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

UPDATE papers
SET visibility_scope = 'public'
WHERE source_type IN ('arxiv', 'semantic_scholar', 'openalex', 'pubmed')
  AND discovery_origin <> 'citation_batch';

COMMENT ON COLUMN papers.visibility_scope IS
    'Server-controlled authorization scope. Public rows are shared; private rows require user_library membership.';
-- 0107: Scope the contradiction uniqueness key to the owning user.
--
-- The evidence-pair key spanned the whole deployment, so the second user to
-- scan a shared paper pair collided with the first user's row and recorded none
-- of their own. Adding the owner widens the key: every existing row keeps its
-- identity because the key only grows, and each user can now hold their own row
-- for the same pair of quotes.
--
-- COALESCE folds legacy owner-less rows into a single bucket. A bare NULL is
-- distinct from every other NULL in a unique index, which would let unowned
-- duplicates accumulate unchecked.
DROP INDEX IF EXISTS idx_paper_contradictions_unique_quotes;

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_contradictions_unique_quotes
    ON paper_contradictions (
        LEAST(paper_a_id, paper_b_id),
        GREATEST(paper_a_id, paper_b_id),
        md5(quote_a),
        md5(quote_b),
        COALESCE(user_id, 0)
    );
-- 0108: Record when a Zotero import's analysis scheduling was resolved, and
-- how many attempts it has spent trying.
--
-- The enqueue happens after the ingest transaction commits, so a failure used
-- to leave a committed paper whose analysis was never scheduled and never
-- could be: the retry path re-runs the upsert, sees an existing row, and the
-- brand-new-paper condition never fires again. Recording the successful enqueue
-- lets a retry tell "already scheduled" from "never scheduled" without
-- re-scheduling every previously imported item on each re-poll.
ALTER TABLE paper_user_zotero_links
    ADD COLUMN IF NOT EXISTS analysis_enqueued_at TIMESTAMPTZ;

-- Treat every link row that predates this column as already resolved. Without
-- this they all read as "decision outstanding", so the first poll after the
-- upgrade re-evaluates the whole imported library. Marking them preserves the
-- behaviour they already had: the previous condition fired only for a brand-new
-- paper, so an existing link could never be re-scheduled regardless.
UPDATE paper_user_zotero_links
   SET analysis_enqueued_at = updated_at
 WHERE analysis_enqueued_at IS NULL;

COMMENT ON COLUMN paper_user_zotero_links.analysis_enqueued_at IS
    'When this import''s analysis scheduling was resolved, by any of the three ways it can resolve: the paper.analyze job was deferred, the import carried no PDF to analyse, or the import spent every attempt allowed and was given up on. It records that a decision was reached, not which one; analysis_enqueue_attempts is what distinguishes an import that was given up on. NULL means the decision is still outstanding and the next poll must make it.';

-- Bound that retrying per item. An unresolved decision pins the library
-- version cursor so the next poll retries it, which is what an enqueue that
-- failed transiently needs — but an enqueue that can never succeed would
-- otherwise stop every other item in the library from syncing forever.
-- Counting attempts on the link row keeps the bound on the item that earned
-- it, so an item that failed once is never given up on because a different
-- item is stuck. Zero is correct for every pre-existing row: the backfill
-- above resolves them all, so they are never scheduled again and can spend
-- no attempt.
ALTER TABLE paper_user_zotero_links
    ADD COLUMN IF NOT EXISTS analysis_enqueue_attempts INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_user_zotero_links.analysis_enqueue_attempts IS
    'How many times this import has tried to schedule its analysis. Incremented inside the ingest transaction, before the paper.analyze deferral runs, so an attempt whose deferral then fails is still counted. Once the limit is reached the poll resolves analysis_enqueued_at and stops retrying that item.';
-- 0109: Track which version of a paper's PDF each derived artifact belongs to.
--
-- Existing papers and artifacts begin together at generation zero. Whenever a
-- verified source replaces a paper's PDF URL, the application increments the
-- paper counter in the same transaction that discards the old derived content.
-- New artifacts copy the paper's current counter so readers can distinguish
-- current evidence from retained work based on a superseded PDF.
ALTER TABLE papers
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN papers.content_generation IS
    'Monotonic version of the PDF-derived content. Incremented atomically when a verified source replacement discards that content.';

ALTER TABLE paper_highlights
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_highlights.content_generation IS
    'The paper content_generation current when this annotation was created. A mismatch means the annotation belongs to a superseded PDF.';

ALTER TABLE paper_summaries
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_summaries.content_generation IS
    'The paper content_generation summarized by this generated result.';

ALTER TABLE paper_extractions
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_extractions.content_generation IS
    'The paper content_generation used for this structured extraction.';

ALTER TABLE paper_entities
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_entities.content_generation IS
    'The paper content_generation from which this entity link was extracted.';

ALTER TABLE entity_relationships
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN entity_relationships.content_generation IS
    'The source paper content_generation supporting this relationship.';

ALTER TABLE paper_notes
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_notes.content_generation IS
    'The paper content_generation displayed when this note was created.';

ALTER TABLE cards
    ADD COLUMN IF NOT EXISTS content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN cards.content_generation IS
    'The source paper content_generation used to create this card.';

ALTER TABLE paper_contradictions
    ADD COLUMN IF NOT EXISTS paper_a_content_generation BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS paper_b_content_generation BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_contradictions.paper_a_content_generation IS
    'The paper A content_generation supporting this evidence pair.';

COMMENT ON COLUMN paper_contradictions.paper_b_content_generation IS
    'The paper B content_generation supporting this evidence pair.';
-- 0110: Require ownership for new contradiction evidence.
--
-- Historical ownerless rows carry no provenance from which an owner could be
-- inferred. Keep those rows intact while rejecting new or changed ownerless
-- evidence. NOT VALID deliberately avoids rewriting or rejecting legacy data.
DO $$
BEGIN
    ALTER TABLE paper_contradictions
        ADD CONSTRAINT chk_paper_contradictions_user_id_present
        CHECK (user_id IS NOT NULL) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

DROP INDEX IF EXISTS idx_paper_contradictions_unique_quotes;

-- New application writes normalize whitespace before insertion. Raw hashes
-- keep historical whitespace variants distinct without mutating or merging
-- their evidence.
CREATE UNIQUE INDEX idx_paper_contradictions_unique_quotes
    ON paper_contradictions (
        LEAST(paper_a_id, paper_b_id),
        GREATEST(paper_a_id, paper_b_id),
        md5(quote_a),
        md5(quote_b),
        COALESCE(user_id, 0),
        paper_a_content_generation,
        paper_b_content_generation
    );
-- Local uploads are identified by the full content digest; the short 16-hex form
-- predates this and is derivable from the stored source URL (`local://<digest>`).
--
-- The NOT EXISTS clause protects the papers_external_id_key unique constraint
-- against a row that already carries the full-digest id for the same content.
-- A row it skips keeps its short id and stays reachable; a later re-upload of
-- those bytes then creates a second, full-id row. That is the accepted residual
-- for the pathological case where both forms of one digest already coexist.
UPDATE papers
SET external_id = 'local:' || substring(url from 9)
WHERE source_type = 'local'
  AND external_id LIKE 'local:%'
  AND length(external_id) = 22
  AND url LIKE 'local://%'
  AND length(url) = 72
  AND NOT EXISTS (
    SELECT 1 FROM papers p2 WHERE p2.external_id = 'local:' || substring(papers.url from 9)
  );
CREATE TABLE focus_sessions (
    id bigserial PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state text NOT NULL CHECK (state IN ('active', 'paused', 'completed')),
    source text NOT NULL CHECK (source IN ('web', 'telegram')),
    duration_seconds integer NOT NULL CHECK (duration_seconds BETWEEN 60 AND 28800),
    started_at timestamp with time zone NOT NULL DEFAULT now(),
    paused_at timestamp with time zone,
    paused_seconds double precision NOT NULL DEFAULT 0 CHECK (paused_seconds >= 0),
    completed_at timestamp with time zone,
    telegram_notified_at timestamp with time zone,
    recorded_seconds double precision NOT NULL DEFAULT 0 CHECK (recorded_seconds >= 0),
    task_id integer REFERENCES tasks(id) ON DELETE SET NULL,
    paper_id integer REFERENCES papers(id) ON DELETE SET NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CHECK ((state = 'paused') = (paused_at IS NOT NULL)),
    CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);

CREATE UNIQUE INDEX focus_sessions_one_open_per_user
    ON focus_sessions (user_id)
    WHERE state IN ('active', 'paused');

CREATE INDEX focus_sessions_user_recent
    ON focus_sessions (user_id, created_at DESC);
-- Restore the project-to-Zotero collection cache used by the push workflow.
-- Older installations that applied migration 031 already have this column;
-- the IF NOT EXISTS keeps those valid upgrade paths idempotent.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS zotero_collection_key TEXT;

COMMENT ON COLUMN projects.zotero_collection_key IS
    'Collection key for this project in its owner''s active Zotero library. Cleared when that library identity changes and resolved again on the next project-linked push.';


-- Install the v1.2.6 physical ownership boundary.  Existing objects move by
-- catalog identity, preserving their data, constraints, indexes, triggers, and rules.

DO $$
DECLARE
    domain record;
    object_name text;
    source_schema text;
    function_record record;
BEGIN
    FOR domain IN
        SELECT * FROM (
            VALUES
                ('platform', 'jarvis_platform_owner'),
                ('research', 'jarvis_research_owner'),
                ('learning', 'jarvis_learning_owner'),
                ('ops', 'jarvis_ops_owner')
        ) AS domains(schema_name, owner_role)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = domain.owner_role) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
                domain.owner_role
            );
        END IF;
        EXECUTE format(
            'ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
            domain.owner_role
        );
        -- Keep each new schema owned by the migration's current role until its
        -- public objects have moved into it.  The legacy owner needs CREATE for
        -- ALTER ... SET SCHEMA, but receives no durable schema privilege.
        IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = domain.schema_name) THEN
            EXECUTE format('CREATE SCHEMA %I', domain.schema_name);
        END IF;
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', domain.schema_name);
    END LOOP;

    -- Runtime shells allow a bootstrap job to assign LOGIN/password separately.
    FOREACH object_name IN ARRAY ARRAY[
        'jarvis_platform_runtime', 'jarvis_research_runtime', 'jarvis_learning_runtime',
        'jarvis_migrator', 'jarvis_legacy_rollback', 'jarvis_backup_reader',
        'jarvis_restore_operator'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = object_name) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
                object_name
            );
        END IF;
        EXECUTE format(
            'ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
            object_name
        );
    END LOOP;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;

    FOR domain IN
        SELECT * FROM (
            VALUES
                ('platform', 'jarvis_platform_owner', ARRAY['audit_log','llm_usage_log','magic_link_tokens','sessions','system_events','telegram_pairing','telegram_pairing_tokens','telegram_user_pairings','user_config','users','webauthn_challenges','webauthn_credentials'], ARRAY['audit_log_id_seq','llm_usage_log_id_seq','system_events_id_seq','user_config_id_seq','users_id_seq'], ARRAY['set_updated_at'], ARRAY[]::text[]),
                ('research', 'jarvis_research_owner', ARRAY['author_alert_log','entities','entity_relationships','extraction_templates','paper_chunks','paper_citations','paper_contradictions','paper_entities','paper_extractions','paper_highlights','paper_notes','paper_recommendations','paper_sources','paper_summaries','paper_topics','paper_user_state','paper_user_zotero_links','papers','pulse_cards','pulse_decks','pulse_models','recommendation_feedback','source_health','source_run_history','thread','topics','tracked_authors','user_library','user_topic_subscriptions'], ARRAY['author_alert_log_id_seq','entities_id_seq','entity_relationships_id_seq','extraction_templates_id_seq','paper_chunks_id_seq','paper_citations_id_seq','paper_contradictions_id_seq','paper_extractions_id_seq','paper_notes_id_seq','paper_recommendations_id_seq','paper_sources_id_seq','paper_summaries_id_seq','paper_user_state_id_seq','papers_id_seq','pulse_cards_id_seq','pulse_decks_id_seq','pulse_models_id_seq','recommendation_feedback_id_seq','source_health_id_seq','source_run_history_id_seq','thread_id_seq','topics_id_seq','tracked_authors_id_seq'], ARRAY['papers_search_vector_update'], ARRAY[]::text[]),
                ('learning', 'jarvis_learning_owner', ARRAY['cards','daily_intent','daily_log','decks','focus_sessions','journal_entries','milestones','project_papers','project_questions','projects','review_logs','scheduled_nudges','task_paper_links','tasks'], ARRAY['cards_id_seq','daily_log_id_seq','decks_id_seq','journal_entries_id_seq','milestones_id_seq','project_questions_id_seq','projects_id_seq','review_logs_id_seq','scheduled_nudges_id_seq','tasks_id_seq'], ARRAY[]::text[], ARRAY[]::text[]),
                ('ops', 'jarvis_ops_owner', ARRAY['job_progress','procrastinate_events','procrastinate_jobs','procrastinate_periodic_defers','procrastinate_workers','schema_migrations'], ARRAY['procrastinate_events_id_seq','procrastinate_jobs_id_seq','procrastinate_periodic_defers_id_seq'], ARRAY['procrastinate_cancel_job_v1','procrastinate_defer_jobs_v1','procrastinate_defer_periodic_job_v2','procrastinate_fetch_job_v2','procrastinate_finish_job_v1','procrastinate_notify_queue_abort_job_v1','procrastinate_notify_queue_job_inserted_v1','procrastinate_prune_stalled_workers_v1','procrastinate_register_worker_v1','procrastinate_retry_job_v1','procrastinate_retry_job_v2','procrastinate_trigger_abort_requested_events_procedure_v1','procrastinate_trigger_function_scheduled_events_v1','procrastinate_trigger_function_status_events_insert_v1','procrastinate_trigger_function_status_events_update_v1','procrastinate_unlink_periodic_defers_v1','procrastinate_unregister_worker_v1','procrastinate_update_heartbeat_v1'], ARRAY['procrastinate_job_event_type','procrastinate_job_status','procrastinate_job_to_defer_v1'])
        ) AS domains(schema_name, owner_role, tables, sequences, functions, types)
    LOOP
        FOREACH object_name IN ARRAY domain.tables LOOP
            SELECT n.nspname INTO source_schema
            FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE c.relname = object_name AND c.relkind IN ('r', 'p')
            ORDER BY CASE n.nspname WHEN domain.schema_name THEN 0 WHEN 'public' THEN 1 ELSE 2 END
            LIMIT 1;
            IF source_schema = 'public' THEN
                EXECUTE format('ALTER TABLE public.%I SET SCHEMA %I', object_name, domain.schema_name);
            END IF;
            EXECUTE format('ALTER TABLE %I.%I OWNER TO %I', domain.schema_name, object_name, domain.owner_role);
        END LOOP;

        FOREACH object_name IN ARRAY domain.types LOOP
            SELECT n.nspname INTO source_schema
            FROM pg_type AS t JOIN pg_namespace AS n ON n.oid = t.typnamespace
            WHERE t.typname = object_name AND t.typtype IN ('c', 'e')
            LIMIT 1;
            IF source_schema = 'public' THEN
                EXECUTE format('ALTER TYPE public.%I SET SCHEMA %I', object_name, domain.schema_name);
            END IF;
            EXECUTE format('ALTER TYPE %I.%I OWNER TO %I', domain.schema_name, object_name, domain.owner_role);
        END LOOP;

        FOR function_record IN
            SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) AS arguments
            FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE p.proname = ANY(domain.functions)
              AND n.nspname IN ('public', domain.schema_name)
        LOOP
            IF function_record.nspname = 'public' THEN
                EXECUTE format('ALTER FUNCTION public.%I(%s) SET SCHEMA %I', function_record.proname, function_record.arguments, domain.schema_name);
            END IF;
            EXECUTE format('ALTER FUNCTION %I.%I(%s) OWNER TO %I', domain.schema_name, function_record.proname, function_record.arguments, domain.owner_role);
        END LOOP;
    END LOOP;

    FOR domain IN
        SELECT * FROM (
            VALUES
                ('platform', 'jarvis_platform_owner'),
                ('research', 'jarvis_research_owner'),
                ('learning', 'jarvis_learning_owner'),
                ('ops', 'jarvis_ops_owner')
        ) AS domains(schema_name, owner_role)
    LOOP
        EXECUTE format('ALTER SCHEMA %I OWNER TO %I', domain.schema_name, domain.owner_role);
    END LOOP;
END $$;

ALTER TABLE ops.schema_migrations ADD COLUMN IF NOT EXISTS sha256 text;
UPDATE ops.schema_migrations
SET sha256 = CASE version
    WHEN 102 THEN 'f51c3cc62b967d409df7499ead7a94048c541ac268d8e7cb447d9b03abe7bdfa'
    WHEN 103 THEN '226474caf5f3a430fc3f4d743d1281e7bc0bad2d8980aa40c1f2924a3774dbc0'
    WHEN 104 THEN '769d1200264aa4c0beb3b6ad36f962a1a0dc520a86712e82b15f26eaa2c79739'
    WHEN 105 THEN 'b4efa848f481af8f2fa90d245c6121f1dc90ecc484001882e547aaa1b4073aa3'
    WHEN 106 THEN 'b57b13bbc0a5ca75d4dabf76f59933600729c5052a0c9588bd47e1c42c25a0c1'
    WHEN 107 THEN '46172fdc5701b936ad2883a0bf1cff2c88acc3f828ab04502d23bb54a5978283'
    WHEN 108 THEN '3258edd25894f27cf4ba3c30f743138229219ddd763838200c6e93d41a49f193'
    WHEN 109 THEN '1c4396cba72df96c0cd86eeff08f073bc6868a7bed8ecc2633e6a722bfdaf53b'
    WHEN 110 THEN '1b18c151f5d578167a4287fc6186539e91838825982ce5d362d205a7768e538c'
    WHEN 111 THEN '2948ac6cef69a389a43cc86493b81bfab3ebf35084c5a95a2092d95e3e29db87'
    WHEN 112 THEN 'f6fe06effb8f3c3df36baa07e0a065a25ff102c5bec192df2c826367d4c1a200'
    WHEN 113 THEN '46898962d09327d0c7ffd5ac13d272d8e092ea41a9ca1c664436b2f6a35431f6'
END
WHERE version BETWEEN 102 AND 113;
DO $$
BEGIN
    ALTER TABLE ops.schema_migrations
        ADD CONSTRAINT schema_migrations_sha256_format
        CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$') NOT VALID;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
ALTER TABLE ops.schema_migrations VALIDATE CONSTRAINT schema_migrations_sha256_format;

DO $$
DECLARE
    domain record;
    object_name text;
BEGIN
    FOR domain IN
        SELECT * FROM (
            VALUES
                ('platform', 'jarvis_platform_owner', 'jarvis_platform_runtime'),
                ('research', 'jarvis_research_owner', 'jarvis_research_runtime'),
                ('learning', 'jarvis_learning_owner', 'jarvis_learning_runtime'),
                ('ops', 'jarvis_ops_owner', NULL::text)
        ) AS domains(schema_name, owner_role, runtime_role)
    LOOP
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO jarvis_legacy_rollback', domain.schema_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO jarvis_legacy_rollback', domain.schema_name);
        EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO jarvis_legacy_rollback', domain.schema_name);
        EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO jarvis_legacy_rollback', domain.schema_name);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO jarvis_backup_reader, jarvis_restore_operator', domain.schema_name);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO jarvis_backup_reader, jarvis_restore_operator', domain.schema_name);
        EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO jarvis_backup_reader, jarvis_restore_operator', domain.schema_name);
        IF domain.runtime_role IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', domain.schema_name, domain.runtime_role);
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I', domain.schema_name, domain.runtime_role);
            EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', domain.schema_name, domain.runtime_role);
            EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', domain.schema_name, domain.runtime_role);
        END IF;
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON TABLES FROM PUBLIC', domain.owner_role, domain.schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM PUBLIC', domain.owner_role, domain.schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC', domain.owner_role, domain.schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE USAGE ON TYPES FROM PUBLIC', domain.owner_role, domain.schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO jarvis_backup_reader, jarvis_restore_operator', domain.owner_role, domain.schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO jarvis_backup_reader, jarvis_restore_operator', domain.owner_role, domain.schema_name);
        IF domain.runtime_role IS NOT NULL THEN
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', domain.owner_role, domain.schema_name, domain.runtime_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO %I', domain.owner_role, domain.schema_name, domain.runtime_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO %I', domain.owner_role, domain.schema_name, domain.runtime_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE ON TYPES TO %I', domain.owner_role, domain.schema_name, domain.runtime_role);
        END IF;
    END LOOP;

    -- Transitional seams are intentionally relation-specific until their API replacements land.
    FOREACH object_name IN ARRAY ARRAY['audit_log','magic_link_tokens','sessions','system_events','user_config','users','webauthn_challenges'] LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON platform.%I TO jarvis_research_runtime', object_name);
    END LOOP;
    FOREACH object_name IN ARRAY ARRAY['cards','daily_log','decks','journal_entries','milestones','project_papers','projects','review_logs','scheduled_nudges','task_paper_links','tasks'] LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON learning.%I TO jarvis_research_runtime', object_name);
    END LOOP;
    FOREACH object_name IN ARRAY ARRAY['job_progress','procrastinate_events','procrastinate_jobs','procrastinate_periodic_defers','procrastinate_workers'] LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ops.%I TO jarvis_research_runtime, jarvis_learning_runtime', object_name);
    END LOOP;
    FOREACH object_name IN ARRAY ARRAY['llm_usage_log','user_config','users'] LOOP
        EXECUTE format('GRANT SELECT ON platform.%I TO jarvis_learning_runtime', object_name);
    END LOOP;
    GRANT INSERT, UPDATE ON platform.user_config TO jarvis_learning_runtime;
    FOREACH object_name IN ARRAY ARRAY['paper_chunks','paper_recommendations','paper_summaries','paper_user_state','paper_user_zotero_links','papers','thread','user_library'] LOOP
        EXECUTE format('GRANT SELECT ON research.%I TO jarvis_learning_runtime', object_name);
    END LOOP;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA platform, learning, ops TO jarvis_research_runtime;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA platform, research, ops TO jarvis_learning_runtime;
    GRANT USAGE ON SCHEMA ops TO jarvis_platform_runtime;
    GRANT USAGE ON SCHEMA platform, learning, ops TO jarvis_research_runtime;
    GRANT USAGE ON SCHEMA platform, research, ops TO jarvis_learning_runtime;
    GRANT SELECT ON ops.schema_migrations TO jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ops TO jarvis_research_runtime, jarvis_learning_runtime;
    GRANT USAGE ON SCHEMA ops TO jarvis_migrator;
    GRANT SELECT, INSERT ON ops.schema_migrations TO jarvis_migrator;

    GRANT jarvis_platform_owner, jarvis_research_owner, jarvis_learning_owner, jarvis_ops_owner
        TO jarvis_migrator;

    ALTER ROLE jarvis_platform_owner SET search_path TO platform, pg_catalog;
    ALTER ROLE jarvis_research_owner SET search_path TO research, pg_catalog;
    ALTER ROLE jarvis_learning_owner SET search_path TO learning, pg_catalog;
    ALTER ROLE jarvis_ops_owner SET search_path TO ops, pg_catalog;
    ALTER ROLE jarvis_platform_runtime SET search_path TO platform, ops, public, pg_catalog;
    ALTER ROLE jarvis_research_runtime SET search_path TO research, platform, learning, ops, public, pg_catalog;
    ALTER ROLE jarvis_learning_runtime SET search_path TO learning, research, platform, ops, public, pg_catalog;
    ALTER ROLE jarvis_migrator SET search_path TO ops, platform, research, learning, public, pg_catalog;
    ALTER ROLE jarvis_legacy_rollback SET search_path TO platform, research, learning, ops, public, pg_catalog;
END $$;

-- The migration runner records this migration after this statement.  Keep its
-- unqualified compatibility insert resolving to the moved metadata relation.
SET search_path TO ops, public, pg_catalog;

UPDATE ops.schema_migrations
SET sha256 = '2380f76ef37b0c6a0aa15c3a55cffbe7365ede9bfe5d8f22f4c1a72fde334a24'
WHERE version = 114;

-- 0115: owner-local cross-domain commands and Platform erasure coordination.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jarvis_erasure_executor') THEN
        CREATE ROLE jarvis_erasure_executor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;
SET ROLE jarvis_research_owner;
CREATE TABLE IF NOT EXISTS research.domain_events (
    id uuid PRIMARY KEY,
    event_type text NOT NULL CHECK (event_type IN ('paper.read', 'paper.deleted')),
    user_id bigint NOT NULL,
    paper_id bigint NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT NOW(),
    last_error text,
    delivered_at timestamptz,
    dead_lettered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS research_domain_events_pending_idx
    ON research.domain_events (next_attempt_at, created_at)
    WHERE delivered_at IS NULL AND dead_lettered_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS research_domain_events_active_deletion_idx
    ON research.domain_events (event_type, user_id, paper_id)
    WHERE event_type = 'paper.deleted'
      AND delivered_at IS NULL AND dead_lettered_at IS NULL;
CREATE TABLE IF NOT EXISTS research.pending_paper_deletions (
    event_id uuid PRIMARY KEY REFERENCES research.domain_events (id) ON DELETE CASCADE,
    user_id bigint NOT NULL, paper_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS research.zotero_push_claims (
    paper_id bigint NOT NULL, user_id bigint NOT NULL, lease_id uuid NOT NULL,
    lease_expires_at timestamptz NOT NULL, PRIMARY KEY (paper_id, user_id)
);
CREATE OR REPLACE FUNCTION research.erase_user_data(p_user_id bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = research, pg_catalog
AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_user_id <= 0 THEN
        RAISE EXCEPTION 'research erasure caller is not allowed';
    END IF;
    DELETE FROM research.pending_paper_deletions WHERE user_id = p_user_id;
    DELETE FROM research.domain_events WHERE user_id = p_user_id;
    DELETE FROM research.zotero_push_claims WHERE user_id = p_user_id;
    DELETE FROM research.author_alert_log WHERE user_id = p_user_id;
    DELETE FROM research.paper_contradictions WHERE user_id = p_user_id;
    DELETE FROM research.paper_entities WHERE user_id = p_user_id;
    DELETE FROM research.paper_extractions WHERE user_id = p_user_id;
    DELETE FROM research.paper_highlights WHERE user_id = p_user_id;
    DELETE FROM research.paper_notes WHERE user_id = p_user_id;
    DELETE FROM research.paper_recommendations WHERE user_id = p_user_id;
    DELETE FROM research.paper_summaries WHERE user_id = p_user_id;
    DELETE FROM research.paper_user_state WHERE user_id = p_user_id;
    DELETE FROM research.paper_user_zotero_links WHERE user_id = p_user_id;
    DELETE FROM research.pulse_cards WHERE user_id = p_user_id;
    DELETE FROM research.pulse_decks WHERE user_id = p_user_id;
    DELETE FROM research.pulse_models WHERE user_id = p_user_id;
    DELETE FROM research.recommendation_feedback WHERE user_id = p_user_id;
    DELETE FROM research.source_health WHERE user_id = p_user_id;
    DELETE FROM research.source_run_history WHERE user_id = p_user_id;
    DELETE FROM research.thread WHERE user_id = p_user_id;
    DELETE FROM research.tracked_authors WHERE user_id = p_user_id;
    DELETE FROM research.user_library WHERE user_id = p_user_id;
    DELETE FROM research.user_topic_subscriptions WHERE user_id = p_user_id;
END;
$$;
REVOKE ALL ON FUNCTION research.erase_user_data(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION research.erase_user_data(bigint) TO jarvis_research_runtime;

SET ROLE jarvis_learning_owner;
CREATE TABLE IF NOT EXISTS learning.domain_commands (
    id uuid PRIMARY KEY,
    command_type text NOT NULL CHECK (command_type IN (
        'paper.read', 'paper.deleted', 'project.zotero_collection', 'journal.upsert', 'user.erase'
    )),
    request_id text NOT NULL,
    user_id bigint,
    paper_id bigint,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    received_at timestamptz NOT NULL DEFAULT NOW(),
    processed_at timestamptz,
    acknowledgement_at timestamptz,
    last_error text,
    UNIQUE (command_type, request_id)
);
CREATE INDEX IF NOT EXISTS learning_domain_commands_pending_idx
    ON learning.domain_commands (received_at) WHERE processed_at IS NULL;
CREATE OR REPLACE FUNCTION learning.erase_user_data(p_user_id bigint, p_request_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = learning, pg_catalog
AS $$
BEGIN
    IF session_user <> 'jarvis_learning_runtime' OR p_user_id <= 0 OR p_request_id = '' THEN
        RAISE EXCEPTION 'learning erasure caller is not allowed';
    END IF;
    DELETE FROM learning.review_logs WHERE user_id = p_user_id;
    DELETE FROM learning.cards WHERE user_id = p_user_id;
    DELETE FROM learning.task_paper_links AS link USING learning.tasks AS task
    WHERE link.task_id = task.id AND task.user_id = p_user_id;
    DELETE FROM learning.tasks WHERE user_id = p_user_id;
    DELETE FROM learning.project_papers AS link USING learning.projects AS project
    WHERE link.project_id = project.id AND project.user_id = p_user_id;
    DELETE FROM learning.project_questions WHERE user_id = p_user_id;
    DELETE FROM learning.milestones WHERE user_id = p_user_id;
    DELETE FROM learning.projects WHERE user_id = p_user_id;
    DELETE FROM learning.decks WHERE user_id = p_user_id;
    DELETE FROM learning.daily_intent WHERE user_id = p_user_id;
    DELETE FROM learning.daily_log WHERE user_id = p_user_id;
    DELETE FROM learning.focus_sessions WHERE user_id = p_user_id;
    DELETE FROM learning.journal_entries WHERE user_id = p_user_id;
    DELETE FROM learning.domain_commands
    WHERE user_id = p_user_id AND request_id <> p_request_id;
    UPDATE learning.domain_commands SET user_id = NULL, paper_id = NULL, payload = '{}'::jsonb
    WHERE command_type = 'user.erase' AND request_id = p_request_id AND user_id = p_user_id;
END;
$$;
REVOKE ALL ON FUNCTION learning.erase_user_data(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION learning.erase_user_data(bigint, text) TO jarvis_learning_runtime;

SET ROLE jarvis_platform_owner;
CREATE TABLE IF NOT EXISTS platform.erasure_requests (
    request_id uuid PRIMARY KEY,
    user_id bigint NOT NULL,
    state text NOT NULL CHECK (state IN (
        'requested', 'qdrant_pending', 'research_pending', 'learning_pending', 'ready',
        'executing', 'complete', 'retry_wait', 'attention_required'
    )),
    resume_state text NOT NULL CHECK (resume_state IN (
        'qdrant_pending', 'research_pending', 'learning_pending', 'executing'
    )),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 8),
    next_attempt_at timestamptz NOT NULL DEFAULT NOW(),
    last_error text,
    requested_at timestamptz NOT NULL DEFAULT NOW(),
    eligible_at timestamptz NOT NULL DEFAULT NOW() + INTERVAL '30 days',
    completed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS platform_erasure_requests_one_active_user_idx
    ON platform.erasure_requests (user_id)
    WHERE state NOT IN ('complete', 'attention_required');
CREATE TABLE IF NOT EXISTS platform.erasure_acknowledgements (
    request_id uuid NOT NULL REFERENCES platform.erasure_requests (request_id) ON DELETE CASCADE,
    domain text NOT NULL CHECK (domain IN ('qdrant', 'research', 'learning')),
    receipt jsonb NOT NULL DEFAULT '{}'::jsonb,
    acknowledged_at timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (request_id, domain)
);
CREATE TABLE IF NOT EXISTS platform.audit_subjects (
    id uuid PRIMARY KEY,
    user_id bigint UNIQUE,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW()
);
ALTER TABLE platform.audit_log ADD COLUMN IF NOT EXISTS subject_id uuid;
ALTER TABLE platform.audit_log ADD COLUMN IF NOT EXISTS caller_role text;
CREATE INDEX IF NOT EXISTS audit_log_subject_id_idx
    ON platform.audit_log (subject_id) WHERE subject_id IS NOT NULL;
ALTER TABLE platform.audit_log DISABLE RULE no_update_audit_log;
UPDATE platform.audit_log SET caller_role = 'jarvis_migrator' WHERE caller_role IS NULL;
INSERT INTO platform.audit_subjects (id, user_id)
SELECT md5('audit-subject:' || user_id)::uuid, user_id::bigint
FROM platform.audit_log
WHERE user_id ~ '^[0-9]+$'
ON CONFLICT (user_id) DO NOTHING;
UPDATE platform.audit_log AS event
SET subject_id = subject.id,
    user_id = NULL,
    metadata = event.metadata - ARRAY[
        'ip', 'client_ip', 'raw_client_ip', 'email', 'name', 'username',
        'telegram_username', 'user_agent', 'user_id'
    ]::text[]
FROM platform.audit_subjects AS subject
WHERE event.subject_id IS NULL AND event.user_id = subject.user_id::text;
UPDATE platform.audit_log SET user_id = NULL,
    metadata = metadata - ARRAY['ip', 'client_ip', 'raw_client_ip', 'email', 'name', 'username',
        'telegram_username', 'user_agent', 'user_id']::text[]
WHERE user_id IS NOT NULL;
ALTER TABLE platform.audit_log ENABLE RULE no_update_audit_log;
ALTER TABLE platform.audit_log ALTER COLUMN caller_role SET NOT NULL;
ALTER TABLE platform.audit_log ADD CONSTRAINT audit_log_caller_role_check CHECK (
    caller_role IN (
        'jarvis_migrator', 'jarvis_platform_runtime',
        'jarvis_research_runtime', 'jarvis_learning_runtime'
    )
);
CREATE OR REPLACE FUNCTION platform.append_audit_event(
    p_user_id text, p_action text, p_resource text, p_metadata jsonb
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog
AS $$
DECLARE
    v_subject_id uuid;
BEGIN
    IF session_user NOT IN ('jarvis_platform_runtime', 'jarvis_research_runtime', 'jarvis_learning_runtime') THEN
        RAISE EXCEPTION 'audit caller is not allowed';
    END IF;
    IF p_action !~ '^[a-z][a-z0-9_.:-]{0,127}$'
       OR p_resource !~ '^/?[a-z][a-z0-9_./:-]{0,255}$'
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
       OR EXISTS (SELECT 1 FROM jsonb_each(COALESCE(p_metadata, '{}'::jsonb)) AS item(key, value)
                  WHERE item.key !~ '^[a-z_][a-z0-9_]{0,63}$'
                     OR jsonb_typeof(item.value) NOT IN ('boolean', 'number', 'null')) THEN
        RAISE EXCEPTION 'audit event has unsafe shape';
    END IF;
    IF p_user_id ~ '^[0-9]+$' THEN
        SELECT id INTO v_subject_id FROM platform.audit_subjects WHERE user_id = p_user_id::bigint;
        IF v_subject_id IS NULL THEN
            v_subject_id := gen_random_uuid();
            INSERT INTO platform.audit_subjects (id, user_id)
            VALUES (v_subject_id, p_user_id::bigint)
            ON CONFLICT (user_id) DO UPDATE SET updated_at = NOW()
            RETURNING id INTO v_subject_id;
        END IF;
    END IF;
    INSERT INTO platform.audit_log (
        subject_id, user_id, caller_role, action, resource, metadata
    ) VALUES (
        v_subject_id, NULL, session_user, p_action, p_resource,
        COALESCE(p_metadata, '{}'::jsonb)
    );
END;
$$;
REVOKE ALL ON FUNCTION platform.append_audit_event(text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.append_audit_event(text, text, text, jsonb)
    TO jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
REVOKE INSERT, UPDATE, DELETE ON platform.audit_log, platform.audit_subjects
    FROM jarvis_platform_runtime;
CREATE OR REPLACE FUNCTION platform.finalize_erasure(p_request_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog
AS $$
DECLARE v_user_id bigint;
BEGIN
    IF session_user <> 'jarvis_erasure_executor' THEN RAISE EXCEPTION 'erasure finalizer is executor-only'; END IF;
    SELECT er.user_id INTO v_user_id
    FROM platform.erasure_requests AS er
    JOIN platform.users AS users ON users.id = er.user_id
    WHERE er.request_id = p_request_id AND er.state = 'ready'
      AND er.eligible_at <= NOW() AND users.deleted_at IS NOT NULL
      AND users.deleted_at + INTERVAL '30 days' <= NOW()
    FOR UPDATE OF er, users;
    IF v_user_id IS NULL THEN RETURN FALSE; END IF;
    UPDATE platform.erasure_requests SET state = 'executing'
    WHERE request_id = p_request_id AND state = 'ready';
    IF (SELECT count(*) FROM platform.erasure_acknowledgements WHERE request_id = p_request_id AND domain IN ('qdrant', 'research', 'learning')) <> 3
       OR COALESCE((SELECT (receipt->>'residual_points')::int FROM platform.erasure_acknowledgements WHERE request_id = p_request_id AND domain = 'qdrant'), -1) <> 0 THEN
        RAISE EXCEPTION 'erasure acknowledgements are incomplete';
    END IF;
    DELETE FROM platform.audit_subjects WHERE user_id = v_user_id;
    DELETE FROM platform.users WHERE id = v_user_id AND deleted_at IS NOT NULL;
    IF NOT FOUND THEN RAISE EXCEPTION 'erasure account is no longer disabled'; END IF;
    UPDATE platform.erasure_requests SET state = 'complete', completed_at = NOW()
    WHERE request_id = p_request_id AND state = 'executing';
    RETURN TRUE;
END;
$$;
REVOKE ALL ON FUNCTION platform.finalize_erasure(uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA platform TO jarvis_erasure_executor;
GRANT EXECUTE ON FUNCTION platform.finalize_erasure(uuid) TO jarvis_erasure_executor;
RESET ROLE;
SET search_path TO ops, public, pg_catalog;

UPDATE ops.schema_migrations
SET sha256 = '852dc3ab061d731179d1cd53714887eca328db436eb6e2a4891d98bb1a1e6bcc'
WHERE version = 115;

-- 0116: Platform-owned unified jobs facade and durable queue ownership.
SET ROLE jarvis_ops_owner;
CREATE TABLE ops.job_owner_registry (
    task_name text PRIMARY KEY,
    queue_name text NOT NULL,
    service_name text NOT NULL CHECK (service_name IN ('research', 'learning')),
    CHECK ((service_name = 'research' AND queue_name = 'paper_ingestion')
        OR (service_name = 'learning' AND queue_name = 'learning_engine'))
);
INSERT INTO ops.job_owner_registry (task_name, queue_name, service_name) VALUES
    ('paper.process','paper_ingestion','research'), ('paper.analyze','paper_ingestion','research'),
    ('papers.batch_process','paper_ingestion','research'), ('papers.batch_summarize','paper_ingestion','research'),
    ('papers.process_library','paper_ingestion','research'), ('papers.scan_local','paper_ingestion','research'),
    ('paper.summarize','paper_ingestion','research'), ('citations.batch_fetch','paper_ingestion','research'),
    ('digest.weekly','paper_ingestion','research'), ('extraction.single','paper_ingestion','research'),
    ('extraction.batch','paper_ingestion','research'), ('contradictions.scan','paper_ingestion','research'),
    ('pulse.generate','paper_ingestion','research'), ('pulse.train_classifier','paper_ingestion','research'),
    ('model.pull','paper_ingestion','research'), ('zotero.push','paper_ingestion','research'),
    ('zotero.resync','paper_ingestion','research'), ('zotero.sync_from_zotero','paper_ingestion','research'),
    ('zotero.sync_annotations','paper_ingestion','research'), ('zotero.push_highlights','paper_ingestion','research'),
    ('card.generate','learning_engine','learning'), ('card.generate_batch','learning_engine','learning');
ALTER TABLE ops.procrastinate_jobs ADD COLUMN owner_queue text, ADD COLUMN owner_service text;
CREATE OR REPLACE FUNCTION ops.enforce_job_owner_metadata_v1() RETURNS trigger
LANGUAGE plpgsql SET search_path = ops, pg_catalog AS $$
DECLARE owner_record ops.job_owner_registry%ROWTYPE;
BEGIN
    IF NOT (NEW.args ? 'job_id') THEN RETURN NEW; END IF;
    SELECT * INTO owner_record FROM ops.job_owner_registry WHERE task_name = NEW.task_name;
    IF NOT FOUND OR NEW.queue_name <> owner_record.queue_name THEN
        RAISE EXCEPTION 'job queue does not match task owner';
    END IF;
    IF (NEW.owner_queue IS NOT NULL AND NEW.owner_queue <> owner_record.queue_name)
       OR (NEW.owner_service IS NOT NULL AND NEW.owner_service <> owner_record.service_name) THEN
        RAISE EXCEPTION 'job owner metadata does not match task owner';
    END IF;
    NEW.owner_queue := owner_record.queue_name; NEW.owner_service := owner_record.service_name;
    RETURN NEW;
END; $$;
CREATE TRIGGER procrastinate_jobs_owner_guard_v1 BEFORE INSERT OR UPDATE OF queue_name, task_name, owner_queue, owner_service
ON ops.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION ops.enforce_job_owner_metadata_v1();
CREATE OR REPLACE VIEW ops.jarvis_jobs_rollback_v1 AS
SELECT args->>'job_id' AS id, task_name AS kind, args->>'user_id' AS user_id,
       args - 'job_id' - 'user_id' AS payload, status::text AS raw_status
FROM ops.procrastinate_jobs WHERE args ? 'job_id';
CREATE OR REPLACE FUNCTION ops.jarvis_job_read_v1(p_job_id text)
RETURNS TABLE (id bigint, queue_name varchar, task_name varchar, status ops.procrastinate_job_status, args jsonb, attempts integer, progress real, progress_message text, result jsonb, error jsonb, created_at timestamptz, started_at timestamptz, finished_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
    SELECT job.id, job.queue_name, job.task_name, job.status, job.args, job.attempts, progress.progress, progress.message, progress.result, progress.error,
      (SELECT min(event.at) FROM ops.procrastinate_events event WHERE event.job_id = job.id),
      (SELECT min(event.at) FROM ops.procrastinate_events event WHERE event.job_id = job.id AND event.type = 'started'),
      (SELECT max(event.at) FROM ops.procrastinate_events event WHERE event.job_id = job.id AND event.type IN ('succeeded','failed','cancelled','aborted'))
    FROM ops.procrastinate_jobs job LEFT JOIN ops.job_progress progress ON progress.jarvis_job_id = job.args->>'job_id'
    WHERE job.args->>'job_id' = p_job_id ORDER BY job.id DESC LIMIT 1 $$;
CREATE OR REPLACE FUNCTION ops.jarvis_job_list_v1(p_status text, p_kind text, p_user_id text, p_limit integer)
RETURNS TABLE (id text, kind varchar, user_id text, status text, payload jsonb, result jsonb, error jsonb, progress double precision, progress_message text, created_at timestamptz, started_at timestamptz, finished_at timestamptz, source text)
LANGUAGE sql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
    SELECT job.args->>'job_id', job.task_name, job.args->>'user_id', CASE job.status WHEN 'todo' THEN 'queued' WHEN 'doing' THEN 'running' WHEN 'aborting' THEN 'running' WHEN 'aborted' THEN 'cancelled' ELSE job.status::text END, job.args - 'job_id' - 'user_id', progress.result, progress.error, COALESCE(progress.progress,0)::double precision, progress.message,
      (SELECT min(event.at) FROM ops.procrastinate_events event WHERE event.job_id = job.id),
      (SELECT min(event.at) FROM ops.procrastinate_events event WHERE event.job_id = job.id AND event.type = 'started'),
      (SELECT max(event.at) FROM ops.procrastinate_events event WHERE event.job_id = job.id AND event.type IN ('succeeded','failed','cancelled','aborted')), 'procrastinate'
    FROM ops.procrastinate_jobs job LEFT JOIN ops.job_progress progress ON progress.jarvis_job_id = job.args->>'job_id'
    WHERE job.args ? 'job_id' AND (p_kind IS NULL OR job.task_name = p_kind) AND ((p_user_id IS NULL AND job.args->>'user_id' IS NULL) OR job.args->>'user_id' = p_user_id)
      AND (p_status IS NULL OR (p_status = 'active' AND job.status IN ('todo','doing','aborting'))
        OR (p_status = 'queued' AND job.status = 'todo') OR (p_status = 'running' AND job.status IN ('doing','aborting'))
        OR (p_status = 'cancelled' AND job.status IN ('cancelled','aborted')) OR p_status = job.status::text)
    ORDER BY job.id DESC LIMIT LEAST(GREATEST(p_limit,1),500) $$;
CREATE OR REPLACE FUNCTION ops.jarvis_job_cancel_v1(p_job_id text, p_user_id text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
BEGIN
    UPDATE ops.procrastinate_jobs SET abort_requested = true, status = CASE status WHEN 'todo' THEN 'cancelled'::ops.procrastinate_job_status ELSE status END
    WHERE args->>'job_id' = p_job_id AND args->>'user_id' = p_user_id AND status IN ('todo','doing');
    RETURN FOUND;
END; $$;
REVOKE ALL ON ALL TABLES IN SCHEMA ops FROM jarvis_platform_runtime;
REVOKE ALL ON FUNCTION ops.jarvis_job_read_v1(text), ops.jarvis_job_list_v1(text,text,text,integer), ops.jarvis_job_cancel_v1(text,text) FROM PUBLIC;
GRANT USAGE ON SCHEMA ops TO jarvis_platform_runtime;
GRANT EXECUTE ON FUNCTION ops.jarvis_job_read_v1(text), ops.jarvis_job_list_v1(text,text,text,integer), ops.jarvis_job_cancel_v1(text,text) TO jarvis_platform_runtime;
GRANT SELECT ON ops.jarvis_jobs_rollback_v1 TO jarvis_legacy_rollback;
RESET ROLE;
SET search_path TO ops, public, pg_catalog;
UPDATE ops.schema_migrations SET sha256 = '9bbd93a0176f882062a66674cf9897d49473bfdc181c620a4d79131adb99fca6' WHERE version = 116;

BEGIN;
-- Owner-local configuration delivery and exact cross-domain capabilities.

SET LOCAL ROLE jarvis_platform_owner;

CREATE TABLE platform.config_deliveries (
    scope_user_id bigint NOT NULL CHECK (scope_user_id >= 0),
    actor_user_id bigint CHECK (actor_user_id IS NULL OR actor_user_id > 0),
    key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_.-]{0,127}$'),
    delivery_id uuid NOT NULL UNIQUE,
    user_role text CHECK (user_role IS NULL OR user_role IN ('member', 'admin')),
    session_id text,
    zotero_scope_changed boolean NOT NULL DEFAULT FALSE,
    state text NOT NULL CHECK (state IN ('pending', 'applied', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 8),
    next_attempt_at timestamptz NOT NULL DEFAULT NOW(),
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_user_id, key),
    CHECK (state <> 'pending' OR actor_user_id IS NOT NULL)
);
CREATE INDEX config_deliveries_due_idx
    ON platform.config_deliveries (next_attempt_at, updated_at)
    WHERE state = 'pending';
REVOKE ALL ON platform.config_deliveries FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.config_deliveries
    TO jarvis_platform_runtime;

CREATE OR REPLACE FUNCTION platform.upsert_config_v1(
    p_user_id bigint, p_key text, p_value jsonb, p_encrypted_value bytea
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_platform_runtime' OR p_key !~ '^[a-z][a-z0-9_.-]{0,127}$'
       OR (p_value IS NOT NULL AND p_encrypted_value IS NOT NULL) THEN
        RAISE EXCEPTION 'configuration write is not allowed';
    END IF;
    INSERT INTO platform.user_config (user_id, key, value, encrypted_value)
    VALUES (p_user_id, p_key, p_value, p_encrypted_value)
    ON CONFLICT (user_id, key) DO UPDATE
    SET value = EXCLUDED.value, encrypted_value = EXCLUDED.encrypted_value, updated_at = NOW();
END; $$;

CREATE OR REPLACE FUNCTION platform.reencrypt_config_v1(p_id integer, p_ciphertext bytea)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_platform_runtime' OR p_ciphertext IS NULL THEN
        RAISE EXCEPTION 'configuration re-encryption is not allowed';
    END IF;
    UPDATE platform.user_config SET value = NULL, encrypted_value = p_ciphertext,
        updated_at = NOW() WHERE id = p_id AND value IS NOT NULL AND encrypted_value IS NULL;
END; $$;

CREATE OR REPLACE FUNCTION platform.set_research_config_v1(
    p_user_id bigint, p_key text, p_value jsonb, p_mode text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime'
       OR p_mode NOT IN ('insert', 'upsert', 'delete')
       OR NOT (
           p_key IN ('zotero.last_library_version', 'scheduler.auto_pipeline.last_run',
                     'pulse.classifier_opt_in', 'pulse.cron',
                     'system.models_autoconfigured', 'llm.smart_model', 'llm.fast_model')
           OR p_key ~ '^llm\.[a-zA-Z0-9_.-]{1,128}\.(smart|fast)_num_ctx$'
       ) THEN
        RAISE EXCEPTION 'research configuration capability is not allowed';
    END IF;
    IF p_mode = 'delete' THEN
        DELETE FROM platform.user_config
        WHERE user_id IS NOT DISTINCT FROM p_user_id AND key = p_key;
    ELSIF p_mode = 'insert' THEN
        INSERT INTO platform.user_config (user_id, key, value)
        VALUES (p_user_id, p_key, p_value)
        ON CONFLICT (user_id, key) DO NOTHING;
    ELSE
        INSERT INTO platform.user_config (user_id, key, value)
        VALUES (p_user_id, p_key, p_value)
        ON CONFLICT (user_id, key) DO UPDATE
        SET value = EXCLUDED.value, encrypted_value = NULL, updated_at = NOW();
    END IF;
    RETURN FOUND;
END; $$;

CREATE OR REPLACE FUNCTION platform.append_system_event_v1(
    p_level text, p_category text, p_source text, p_message text,
    p_context jsonb, p_correlation_id uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user NOT IN ('jarvis_platform_runtime', 'jarvis_research_runtime',
                            'jarvis_learning_runtime')
       OR p_level NOT IN ('debug', 'info', 'warning', 'error', 'critical')
       OR p_category NOT IN ('error', 'job', 'source', 'auth', 'config', 'infra')
       OR length(p_source) NOT BETWEEN 1 AND 200
       OR length(p_message) NOT BETWEEN 1 AND 65535
       OR jsonb_typeof(COALESCE(p_context, '{}'::jsonb)) <> 'object'
       OR pg_column_size(COALESCE(p_context, '{}'::jsonb)) > 65536 THEN
        RAISE EXCEPTION 'system event has unsafe shape';
    END IF;
    INSERT INTO platform.system_events
        (level, category, source, message, context, correlation_id)
    VALUES (p_level, p_category, p_source, p_message,
            COALESCE(p_context, '{}'::jsonb), p_correlation_id);
END; $$;

CREATE OR REPLACE FUNCTION platform.mint_session_v1(
    p_user_id bigint, p_expires_at timestamptz, p_credential_id uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE v_id uuid;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' OR p_expires_at <= NOW()
       OR NOT EXISTS (SELECT 1 FROM platform.users WHERE id = p_user_id AND deleted_at IS NULL) THEN
        RAISE EXCEPTION 'session mint is not allowed';
    END IF;
    INSERT INTO platform.sessions (user_id, expires_at, credential_id)
    VALUES (p_user_id, p_expires_at, p_credential_id) RETURNING id INTO v_id;
    RETURN v_id;
END; $$;

CREATE OR REPLACE FUNCTION platform.renew_session_v1(
    p_session_id text, p_ttl interval, p_renew_after interval
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE v_id uuid;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' OR p_ttl <= interval '0'
       OR p_renew_after <= interval '0' OR p_renew_after >= p_ttl THEN
        RAISE EXCEPTION 'session renewal is not allowed';
    END IF;
    UPDATE platform.sessions SET expires_at = NOW() + p_ttl
    WHERE id = p_session_id::uuid AND revoked_at IS NULL AND expires_at > NOW()
      AND expires_at < NOW() + p_ttl - p_renew_after RETURNING id INTO v_id;
    RETURN v_id;
END; $$;

CREATE OR REPLACE FUNCTION platform.purge_identity_retention_v1(p_operation text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE v_count integer;
BEGIN
    IF session_user <> 'jarvis_research_runtime'
       OR p_operation NOT IN ('sessions', 'webauthn_challenges', 'magic_link_tokens') THEN
        RAISE EXCEPTION 'identity retention capability is not allowed';
    END IF;
    IF p_operation = 'sessions' THEN
        DELETE FROM platform.sessions
        WHERE expires_at < NOW() - interval '30 days'
           OR (revoked_at IS NOT NULL AND revoked_at < NOW() - interval '7 days');
    ELSIF p_operation = 'webauthn_challenges' THEN
        DELETE FROM platform.webauthn_challenges WHERE expires_at < NOW();
    ELSE
        DELETE FROM platform.magic_link_tokens WHERE expires_at < NOW() - interval '1 day';
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END; $$;

CREATE OR REPLACE FUNCTION platform.purge_system_events_v1(p_class text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE v_count integer;
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_class NOT IN ('app', 'infra') THEN
        RAISE EXCEPTION 'event retention capability is not allowed';
    END IF;
    IF p_class = 'infra' THEN
        DELETE FROM platform.system_events
        WHERE category = 'infra' AND created_at < NOW() - interval '7 days';
    ELSE
        DELETE FROM platform.system_events
        WHERE category <> 'infra' AND created_at < NOW() - interval '30 days';
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END; $$;

CREATE OR REPLACE FUNCTION platform.rotate_visibility_checkpoint_v1(
    p_key text, p_generation text, p_recovery text, p_rotated_at text
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime'
       OR p_key <> 'qdrant.visibility_checkpoint'
       OR p_generation !~ '^[a-f0-9]{32}$' OR length(p_recovery) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'visibility checkpoint rotation is not allowed';
    END IF;
    RETURN QUERY INSERT INTO platform.user_config(user_id, key, value)
    VALUES (NULL, p_key, jsonb_build_object(
        'version', 1, 'visibility_generation', p_generation, 'status', 'pending',
        'last_chunk_id', 0, 'qdrant_recovery', p_recovery, 'rotated_at', p_rotated_at))
    ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    RETURNING user_config.value;
END; $$;

CREATE OR REPLACE FUNCTION platform.claim_visibility_lease_v1(
    p_key text, p_generation text, p_worker text, p_seconds integer
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_key <> 'qdrant.visibility_checkpoint'
       OR p_worker = '' OR p_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'visibility lease is not allowed';
    END IF;
    RETURN QUERY UPDATE platform.user_config
    SET value = jsonb_set(jsonb_set(user_config.value, '{worker_lease_token}',
            to_jsonb(p_worker), true), '{lease_expires_at}',
            to_jsonb((NOW() + make_interval(secs => p_seconds))::text), true),
        updated_at = NOW()
    WHERE user_id IS NULL AND key = p_key
      AND user_config.value->>'visibility_generation' = p_generation
      AND user_config.value->>'status' = 'pending'
      AND (user_config.value->>'worker_lease_token' IS NULL
           OR user_config.value->>'worker_lease_token' = p_worker
           OR NULLIF(user_config.value->>'lease_expires_at', '')::timestamptz <= NOW())
    RETURNING user_config.value;
END; $$;

CREATE OR REPLACE FUNCTION platform.advance_visibility_checkpoint_v1(
    p_key text, p_generation text, p_worker text, p_chunk bigint, p_seconds integer
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_key <> 'qdrant.visibility_checkpoint'
       OR p_chunk < 0 OR p_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'visibility progress is not allowed';
    END IF;
    RETURN QUERY UPDATE platform.user_config
    SET value = jsonb_set(jsonb_set(user_config.value, '{last_chunk_id}',
            to_jsonb(GREATEST((user_config.value->>'last_chunk_id')::bigint, p_chunk)), true),
            '{lease_expires_at}',
            to_jsonb((NOW() + make_interval(secs => p_seconds))::text), true),
        updated_at = NOW()
    WHERE user_id IS NULL AND key = p_key
      AND user_config.value->>'visibility_generation' = p_generation
      AND user_config.value->>'status' = 'pending'
      AND user_config.value->>'worker_lease_token' = p_worker
      AND NULLIF(user_config.value->>'lease_expires_at', '')::timestamptz > NOW()
    RETURNING user_config.value;
END; $$;

CREATE OR REPLACE FUNCTION platform.complete_visibility_checkpoint_v1(
    p_key text, p_generation text, p_worker text
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_key <> 'qdrant.visibility_checkpoint' THEN
        RAISE EXCEPTION 'visibility completion is not allowed';
    END IF;
    RETURN QUERY UPDATE platform.user_config
    SET value = jsonb_set(user_config.value - 'worker_lease_token' - 'lease_expires_at',
            '{status}', to_jsonb('complete'::text), true), updated_at = NOW()
    WHERE user_id IS NULL AND key = p_key
      AND user_config.value->>'visibility_generation' = p_generation
      AND user_config.value->>'status' = 'pending'
      AND user_config.value->>'worker_lease_token' = p_worker
      AND NULLIF(user_config.value->>'lease_expires_at', '')::timestamptz > NOW()
    RETURNING user_config.value;
END; $$;

REVOKE ALL ON FUNCTION platform.upsert_config_v1(bigint,text,jsonb,bytea),
    platform.reencrypt_config_v1(integer,bytea),
    platform.set_research_config_v1(bigint,text,jsonb,text),
    platform.append_system_event_v1(text,text,text,text,jsonb,uuid),
    platform.mint_session_v1(bigint,timestamptz,uuid),
    platform.renew_session_v1(text,interval,interval),
    platform.purge_identity_retention_v1(text), platform.purge_system_events_v1(text),
    platform.rotate_visibility_checkpoint_v1(text,text,text,text),
    platform.claim_visibility_lease_v1(text,text,text,integer),
    platform.advance_visibility_checkpoint_v1(text,text,text,bigint,integer),
    platform.complete_visibility_checkpoint_v1(text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.upsert_config_v1(bigint,text,jsonb,bytea),
    platform.reencrypt_config_v1(integer,bytea), platform.mint_session_v1(bigint,timestamptz,uuid),
    platform.renew_session_v1(text,interval,interval) TO jarvis_platform_runtime;
GRANT EXECUTE ON FUNCTION platform.set_research_config_v1(bigint,text,jsonb,text),
    platform.purge_identity_retention_v1(text), platform.purge_system_events_v1(text),
    platform.rotate_visibility_checkpoint_v1(text,text,text,text),
    platform.claim_visibility_lease_v1(text,text,text,integer),
    platform.advance_visibility_checkpoint_v1(text,text,text,bigint,integer),
    platform.complete_visibility_checkpoint_v1(text,text,text) TO jarvis_research_runtime;
GRANT EXECUTE ON FUNCTION platform.append_system_event_v1(text,text,text,text,jsonb,uuid)
    TO jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
GRANT USAGE ON SCHEMA platform TO jarvis_research_runtime, jarvis_learning_runtime;

RESET ROLE;

SET LOCAL ROLE jarvis_research_owner;

CREATE OR REPLACE FUNCTION research.record_author_alert_v1(
    p_author_id integer, p_paper_id integer, p_user_id integer
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN RAISE EXCEPTION 'author alert caller is not allowed'; END IF;
    INSERT INTO research.author_alert_log (tracked_author_id, paper_id, user_id)
    VALUES (p_author_id, p_paper_id, p_user_id)
    ON CONFLICT (tracked_author_id, paper_id, user_id) DO NOTHING;
    RETURN FOUND;
END; $$;

CREATE OR REPLACE FUNCTION research.add_to_library_v1(
    p_user_id integer, p_paper_id integer, p_added_via text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN RAISE EXCEPTION 'library caller is not allowed'; END IF;
    INSERT INTO research.user_library (user_id, paper_id, added_via)
    VALUES (p_user_id, p_paper_id, p_added_via) ON CONFLICT (user_id, paper_id) DO NOTHING;
END; $$;

CREATE OR REPLACE FUNCTION research.fan_out_library_v1(
    p_user_ids integer[], p_paper_id integer
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
DECLARE v_count integer;
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR cardinality(p_user_ids) > 10000 THEN
        RAISE EXCEPTION 'library fan-out caller is not allowed';
    END IF;
    INSERT INTO research.user_library (user_id, paper_id, added_via)
    SELECT DISTINCT user_id, p_paper_id, 'auto_fetch_topic_match'
    FROM unnest(p_user_ids) AS user_id
    ON CONFLICT (user_id, paper_id) DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END; $$;

CREATE OR REPLACE FUNCTION research.upsert_paper_state_v1(
    p_paper_id integer, p_user_id integer, p_state text, p_starred boolean, p_mode text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user NOT IN ('jarvis_research_runtime', 'jarvis_learning_runtime')
       OR p_mode NOT IN ('dynamic', 'first_sync', 'advance') THEN
        RAISE EXCEPTION 'paper state caller is not allowed';
    END IF;
    IF session_user = 'jarvis_learning_runtime' THEN
        IF p_user_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM research.papers p WHERE p.id = p_paper_id
              AND (p.visibility_scope = 'public' OR EXISTS (
                  SELECT 1 FROM research.user_library ul
                  WHERE ul.paper_id = p.id AND ul.user_id = p_user_id))
        ) THEN
            RAISE EXCEPTION 'paper state owner mismatch';
        END IF;
    END IF;
    IF p_mode = 'first_sync' THEN
        INSERT INTO research.paper_user_state (paper_id, user_id, state, starred)
        VALUES (p_paper_id, p_user_id, COALESCE(p_state, 'inbox'), COALESCE(p_starred, FALSE))
        ON CONFLICT (paper_id, user_id) DO NOTHING;
    ELSIF p_mode = 'advance' THEN
        INSERT INTO research.paper_user_state (paper_id, user_id, state)
        VALUES (p_paper_id, p_user_id, p_state)
        ON CONFLICT (paper_id, user_id) DO UPDATE SET state = EXCLUDED.state
        WHERE paper_user_state.state IN ('inbox', 'to_read');
    ELSE
        INSERT INTO research.paper_user_state (paper_id, user_id, state, starred)
        VALUES (p_paper_id, p_user_id, COALESCE(p_state, 'inbox'), COALESCE(p_starred, FALSE))
        ON CONFLICT (paper_id, user_id) DO UPDATE SET
            state = COALESCE(p_state, paper_user_state.state),
            starred = COALESCE(p_starred, paper_user_state.starred);
    END IF;
END; $$;

CREATE OR REPLACE FUNCTION research.star_paper_v1(p_paper_id integer, p_user_id integer)
RETURNS TABLE(is_new_row boolean, prev_starred boolean) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN RAISE EXCEPTION 'paper star caller is not allowed'; END IF;
    RETURN QUERY WITH before AS (
        SELECT starred FROM research.paper_user_state
        WHERE paper_id = p_paper_id AND user_id IS NOT DISTINCT FROM p_user_id)
    INSERT INTO research.paper_user_state (paper_id, user_id, starred)
    VALUES (p_paper_id, p_user_id, TRUE)
    ON CONFLICT (paper_id, user_id) DO UPDATE SET starred = TRUE
    RETURNING (xmax = 0), (SELECT COALESCE(starred, FALSE) FROM before);
END; $$;

CREATE OR REPLACE FUNCTION research.update_paper_feedback_v1(
    p_paper_id integer, p_user_id integer, p_rating integer, p_notes text, p_flagged boolean
) RETURNS TABLE(state text, state_before_trash text, starred boolean, rating smallint,
                user_notes text, flagged boolean, updated_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN RAISE EXCEPTION 'paper feedback caller is not allowed'; END IF;
    RETURN QUERY INSERT INTO research.paper_user_state
        (paper_id, user_id, rating, user_notes, flagged)
    VALUES (p_paper_id, p_user_id, p_rating, p_notes, COALESCE(p_flagged, FALSE))
    ON CONFLICT (paper_id, user_id) DO UPDATE SET
        rating = COALESCE(EXCLUDED.rating, paper_user_state.rating),
        user_notes = COALESCE(EXCLUDED.user_notes, paper_user_state.user_notes),
        flagged = COALESCE(p_flagged, paper_user_state.flagged)
    RETURNING COALESCE(paper_user_state.state, 'inbox'), paper_user_state.state_before_trash,
        COALESCE(paper_user_state.starred, FALSE), paper_user_state.rating,
        paper_user_state.user_notes, COALESCE(paper_user_state.flagged, FALSE),
        paper_user_state.updated_at;
END; $$;

CREATE OR REPLACE FUNCTION research.trash_paper_v1(p_paper_id integer, p_user_id integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN RAISE EXCEPTION 'paper trash caller is not allowed'; END IF;
    INSERT INTO research.paper_user_state (paper_id, user_id, state, state_before_trash)
    VALUES (p_paper_id, p_user_id, 'trash', 'inbox')
    ON CONFLICT (paper_id, user_id) DO UPDATE SET state_before_trash = CASE
        WHEN paper_user_state.state = 'trash' THEN paper_user_state.state_before_trash
        ELSE paper_user_state.state END, state = 'trash';
END; $$;

CREATE OR REPLACE FUNCTION research.restore_paper_v1(p_paper_id integer, p_user_id integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN RAISE EXCEPTION 'paper restore caller is not allowed'; END IF;
    UPDATE research.paper_user_state SET state = COALESCE(state_before_trash, 'inbox'),
        state_before_trash = NULL
    WHERE paper_id = p_paper_id AND user_id IS NOT DISTINCT FROM p_user_id;
    RETURN FOUND;
END; $$;

CREATE OR REPLACE FUNCTION research.claim_source_slot_v1(
    p_user_id integer, p_source text, p_min_seconds text
) RETURNS TABLE(last_request_at timestamptz) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_source = ''
       OR p_min_seconds::numeric < 0 OR p_min_seconds::numeric > 86400 THEN
        RAISE EXCEPTION 'source slot caller is not allowed';
    END IF;
    RETURN QUERY INSERT INTO research.source_health
        (user_id, source_type, last_request_at, updated_at)
    VALUES (p_user_id, p_source, NOW(), NOW())
    ON CONFLICT (user_id, source_type) DO UPDATE
    SET last_request_at = NOW(), updated_at = NOW()
    WHERE source_health.last_request_at IS NULL
       OR source_health.last_request_at < NOW() - (p_min_seconds || ' seconds')::interval
    RETURNING source_health.last_request_at;
END; $$;

CREATE OR REPLACE FUNCTION research.update_source_health_v1(
    p_user_id integer, p_source text, p_at timestamptz, p_status text, p_cooldown timestamptz
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = research, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime'
       OR p_status NOT IN ('ok', 'rate_limit', 'error', 'reset') THEN
        RAISE EXCEPTION 'source health caller is not allowed';
    END IF;
    INSERT INTO research.source_health
        (user_id, source_type, last_request_at, last_status, cooldown_until,
         consecutive_failures, updated_at)
    VALUES (p_user_id, p_source, p_at, CASE WHEN p_status = 'reset' THEN 'ok' ELSE p_status END,
            p_cooldown, CASE WHEN p_status = 'error' THEN 1 ELSE 0 END, p_at)
    ON CONFLICT (user_id, source_type) DO UPDATE SET
        last_request_at = CASE WHEN p_status = 'reset' THEN source_health.last_request_at ELSE p_at END,
        last_status = CASE WHEN p_status = 'reset' THEN 'ok' ELSE p_status END,
        cooldown_until = CASE WHEN p_status IN ('ok', 'reset') THEN NULL
                              WHEN p_status = 'rate_limit' THEN p_cooldown
                              ELSE source_health.cooldown_until END,
        consecutive_failures = CASE WHEN p_status = 'error'
            THEN COALESCE(source_health.consecutive_failures, 0) + 1
            WHEN p_status IN ('ok', 'reset') THEN 0 ELSE source_health.consecutive_failures END,
        updated_at = p_at;
END; $$;

REVOKE ALL ON FUNCTION research.record_author_alert_v1(integer,integer,integer),
    research.add_to_library_v1(integer,integer,text),
    research.fan_out_library_v1(integer[],integer),
    research.upsert_paper_state_v1(integer,integer,text,boolean,text),
    research.star_paper_v1(integer,integer),
    research.update_paper_feedback_v1(integer,integer,integer,text,boolean),
    research.trash_paper_v1(integer,integer), research.restore_paper_v1(integer,integer),
    research.claim_source_slot_v1(integer,text,text),
    research.update_source_health_v1(integer,text,timestamptz,text,timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION research.record_author_alert_v1(integer,integer,integer),
    research.add_to_library_v1(integer,integer,text),
    research.fan_out_library_v1(integer[],integer),
    research.upsert_paper_state_v1(integer,integer,text,boolean,text),
    research.star_paper_v1(integer,integer),
    research.update_paper_feedback_v1(integer,integer,integer,text,boolean),
    research.trash_paper_v1(integer,integer), research.restore_paper_v1(integer,integer),
    research.claim_source_slot_v1(integer,text,text),
    research.update_source_health_v1(integer,text,timestamptz,text,timestamptz)
    TO jarvis_research_runtime;
GRANT EXECUTE ON FUNCTION research.upsert_paper_state_v1(integer,integer,text,boolean,text)
    TO jarvis_learning_runtime;
GRANT USAGE ON SCHEMA research TO jarvis_learning_runtime;

RESET ROLE;

SET LOCAL ROLE jarvis_learning_owner;
CREATE OR REPLACE FUNCTION learning.clear_zotero_collection_keys_v1(p_user_id integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = learning, pg_catalog AS $$
DECLARE v_count integer;
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_user_id IS NULL THEN
        RAISE EXCEPTION 'project Zotero cleanup caller is not allowed';
    END IF;
    UPDATE learning.projects SET zotero_collection_key = NULL
    WHERE user_id = p_user_id AND zotero_collection_key IS NOT NULL;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END; $$;
REVOKE ALL ON FUNCTION learning.clear_zotero_collection_keys_v1(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION learning.clear_zotero_collection_keys_v1(integer)
    TO jarvis_research_runtime;
GRANT USAGE ON SCHEMA learning TO jarvis_research_runtime;

RESET ROLE;

SET LOCAL ROLE jarvis_ops_owner;
CREATE OR REPLACE FUNCTION ops.record_job_progress_v1(
    p_job_id text, p_progress real, p_message text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
DECLARE v_owner text;
BEGIN
    v_owner := CASE session_user WHEN 'jarvis_research_runtime' THEN 'research'
        WHEN 'jarvis_learning_runtime' THEN 'learning' ELSE NULL END;
    IF v_owner IS NULL OR NOT EXISTS (SELECT 1 FROM ops.procrastinate_jobs
        WHERE args->>'job_id' = p_job_id AND owner_service = v_owner) THEN
        RAISE EXCEPTION 'job progress owner mismatch';
    END IF;
    INSERT INTO ops.job_progress (jarvis_job_id, progress, message, updated_at)
    VALUES (p_job_id, p_progress, p_message, NOW())
    ON CONFLICT (jarvis_job_id) DO UPDATE SET progress = EXCLUDED.progress,
        message = EXCLUDED.message, updated_at = EXCLUDED.updated_at;
END; $$;

CREATE OR REPLACE FUNCTION ops.record_job_outcome_v1(
    p_job_id text, p_result jsonb, p_error jsonb, p_is_error boolean
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
DECLARE v_owner text;
BEGIN
    v_owner := CASE session_user WHEN 'jarvis_research_runtime' THEN 'research'
        WHEN 'jarvis_learning_runtime' THEN 'learning' ELSE NULL END;
    IF v_owner IS NULL OR NOT EXISTS (SELECT 1 FROM ops.procrastinate_jobs
        WHERE args->>'job_id' = p_job_id AND owner_service = v_owner) THEN
        RAISE EXCEPTION 'job outcome owner mismatch';
    END IF;
    INSERT INTO ops.job_progress (jarvis_job_id, progress, result, error, updated_at)
    VALUES (p_job_id, CASE WHEN p_is_error THEN 0.0 ELSE 1.0 END, p_result, p_error, NOW())
    ON CONFLICT (jarvis_job_id) DO UPDATE SET
        progress = COALESCE(job_progress.progress, EXCLUDED.progress),
        result = EXCLUDED.result, error = EXCLUDED.error, updated_at = EXCLUDED.updated_at;
END; $$;

CREATE OR REPLACE FUNCTION ops.purge_orphaned_job_progress_v1()
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
DECLARE v_count integer;
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN RAISE EXCEPTION 'job retention caller is not allowed'; END IF;
    DELETE FROM ops.job_progress WHERE NOT EXISTS (
        SELECT 1 FROM ops.procrastinate_jobs job
        WHERE job.args->>'job_id' = job_progress.jarvis_job_id);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END; $$;
REVOKE ALL ON FUNCTION ops.record_job_progress_v1(text,real,text),
    ops.record_job_outcome_v1(text,jsonb,jsonb,boolean),
    ops.purge_orphaned_job_progress_v1() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.record_job_progress_v1(text,real,text),
    ops.record_job_outcome_v1(text,jsonb,jsonb,boolean)
    TO jarvis_research_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION ops.purge_orphaned_job_progress_v1()
    TO jarvis_research_runtime;
GRANT USAGE ON SCHEMA ops TO jarvis_research_runtime, jarvis_learning_runtime;

CREATE OR REPLACE FUNCTION ops.enforce_job_owner_metadata_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
DECLARE owner_record ops.job_owner_registry%ROWTYPE; runtime_owner text;
BEGIN
    IF NOT (NEW.args ? 'job_id') THEN RETURN NEW; END IF;
    SELECT * INTO owner_record FROM ops.job_owner_registry WHERE task_name = NEW.task_name;
    IF NOT FOUND OR NEW.queue_name <> owner_record.queue_name THEN
        RAISE EXCEPTION 'job queue does not match task owner';
    END IF;
    runtime_owner := CASE session_user WHEN 'jarvis_research_runtime' THEN 'research'
        WHEN 'jarvis_learning_runtime' THEN 'learning' ELSE NULL END;
    IF runtime_owner IS NOT NULL AND owner_record.service_name <> runtime_owner THEN
        RAISE EXCEPTION 'job task is owned by another runtime';
    END IF;
    IF (NEW.owner_queue IS NOT NULL AND NEW.owner_queue <> owner_record.queue_name)
       OR (NEW.owner_service IS NOT NULL AND NEW.owner_service <> owner_record.service_name) THEN
        RAISE EXCEPTION 'job owner metadata does not match task owner';
    END IF;
    NEW.owner_queue := owner_record.queue_name; NEW.owner_service := owner_record.service_name;
    RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION ops.enforce_job_owner_metadata_v1() FROM PUBLIC;
ALTER TABLE ops.procrastinate_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY research_job_rows_v1 ON ops.procrastinate_jobs
    FOR ALL TO jarvis_research_runtime
    USING (owner_service = 'research' OR (owner_service IS NULL AND queue_name = 'paper_ingestion'))
    WITH CHECK (owner_service = 'research' OR (owner_service IS NULL AND queue_name = 'paper_ingestion'));
CREATE POLICY learning_job_rows_v1 ON ops.procrastinate_jobs
    FOR ALL TO jarvis_learning_runtime
    USING (owner_service = 'learning' OR (owner_service IS NULL AND queue_name = 'learning_engine'))
    WITH CHECK (owner_service = 'learning' OR (owner_service IS NULL AND queue_name = 'learning_engine'));
CREATE OR REPLACE FUNCTION ops.redact_erased_user_jobs_v1(p_user_id bigint)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog AS $$
DECLARE v_count integer;
BEGIN
    IF session_user <> 'jarvis_erasure_executor' OR p_user_id <= 0 THEN
        RAISE EXCEPTION 'job erasure caller is not allowed';
    END IF;
    IF EXISTS (SELECT 1 FROM ops.procrastinate_jobs
               WHERE args->>'user_id' = p_user_id::text
                 AND status IN ('todo', 'doing', 'aborting')) THEN
        RAISE EXCEPTION 'active jobs still retain the erasure subject';
    END IF;
    UPDATE ops.procrastinate_jobs SET args = args - 'user_id'
    WHERE args->>'user_id' = p_user_id::text;
    GET DIAGNOSTICS v_count = ROW_COUNT; RETURN v_count;
END; $$;
REVOKE ALL ON FUNCTION ops.redact_erased_user_jobs_v1(bigint) FROM PUBLIC;
GRANT USAGE ON SCHEMA ops TO jarvis_platform_owner;
GRANT EXECUTE ON FUNCTION ops.redact_erased_user_jobs_v1(bigint) TO jarvis_platform_owner;

RESET ROLE;
SET LOCAL ROLE jarvis_platform_owner;
CREATE OR REPLACE FUNCTION platform.finalize_erasure(p_request_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = platform, pg_catalog AS $$
DECLARE v_user_id bigint;
BEGIN
    IF session_user <> 'jarvis_erasure_executor' THEN RAISE EXCEPTION 'erasure finalizer is executor-only'; END IF;
    SELECT er.user_id INTO v_user_id FROM platform.erasure_requests er
    JOIN platform.users users ON users.id = er.user_id
    WHERE er.request_id = p_request_id AND er.state = 'ready' AND er.eligible_at <= NOW()
      AND users.deleted_at IS NOT NULL AND users.deleted_at + INTERVAL '30 days' <= NOW()
    FOR UPDATE OF er, users;
    IF v_user_id IS NULL THEN RETURN FALSE; END IF;
    UPDATE platform.erasure_requests SET state = 'executing'
    WHERE request_id = p_request_id AND state = 'ready';
    IF (SELECT count(*) FROM platform.erasure_acknowledgements
        WHERE request_id = p_request_id AND domain IN ('qdrant', 'research', 'learning')) <> 3
       OR COALESCE((SELECT (receipt->>'residual_points')::int
                    FROM platform.erasure_acknowledgements
                    WHERE request_id = p_request_id AND domain = 'qdrant'), -1) <> 0 THEN
        RAISE EXCEPTION 'erasure acknowledgements are incomplete';
    END IF;
    PERFORM ops.redact_erased_user_jobs_v1(v_user_id);
    DELETE FROM platform.config_deliveries WHERE scope_user_id = v_user_id;
    UPDATE platform.config_deliveries SET actor_user_id = NULL, state = 'failed', attempts = 8,
        last_error = 'authorizing account erased', updated_at = NOW() WHERE actor_user_id = v_user_id;
    DELETE FROM platform.audit_subjects WHERE user_id = v_user_id;
    DELETE FROM platform.users WHERE id = v_user_id AND deleted_at IS NOT NULL;
    IF NOT FOUND THEN RAISE EXCEPTION 'erasure account is no longer disabled'; END IF;
    UPDATE platform.erasure_requests SET state = 'complete', completed_at = NOW()
    WHERE request_id = p_request_id AND state = 'executing'; RETURN TRUE;
END; $$;
REVOKE ALL ON FUNCTION platform.finalize_erasure(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.finalize_erasure(uuid) TO jarvis_erasure_executor;
RESET ROLE;
SET LOCAL search_path TO ops, public, pg_catalog;
UPDATE ops.schema_migrations SET sha256 = 'd8ff5e67cb30eb0ac0efb6be7e25cc0101b3ee18cd1902e91b8a50cd4954117b' WHERE version = 117;
COMMIT;
