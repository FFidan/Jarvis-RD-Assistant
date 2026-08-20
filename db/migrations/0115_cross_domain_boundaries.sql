-- Durable owner-local commands, projections, and Platform erasure coordination.
-- Runtime services use only the DML surfaces created here.

SET LOCAL ROLE jarvis_ops_owner;
GRANT USAGE ON SCHEMA ops TO jarvis_migrator;
GRANT SELECT, INSERT ON ops.schema_migrations TO jarvis_migrator;
RESET ROLE;

SET LOCAL ROLE jarvis_research_owner;
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
    user_id bigint NOT NULL,
    paper_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS research.zotero_push_claims (
    paper_id bigint NOT NULL,
    user_id bigint NOT NULL,
    lease_id uuid NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    PRIMARY KEY (paper_id, user_id)
);
CREATE OR REPLACE FUNCTION research.erase_user_data(p_user_id bigint)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
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

SET LOCAL ROLE jarvis_learning_owner;
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
    ON learning.domain_commands (received_at)
    WHERE processed_at IS NULL;
CREATE OR REPLACE FUNCTION learning.erase_user_data(p_user_id bigint, p_request_id text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = learning, pg_catalog
AS $$
BEGIN
    IF session_user <> 'jarvis_learning_runtime' OR p_user_id <= 0 OR p_request_id = '' THEN
        RAISE EXCEPTION 'learning erasure caller is not allowed';
    END IF;
    DELETE FROM learning.review_logs WHERE user_id = p_user_id;
    DELETE FROM learning.cards WHERE user_id = p_user_id;
    DELETE FROM learning.task_paper_links AS link
    USING learning.tasks AS task
    WHERE link.task_id = task.id AND task.user_id = p_user_id;
    DELETE FROM learning.tasks WHERE user_id = p_user_id;
    DELETE FROM learning.project_papers AS link
    USING learning.projects AS project
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

SET LOCAL ROLE jarvis_platform_owner;
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

INSERT INTO platform.audit_subjects (id, user_id)
SELECT md5('audit-subject:' || user_id)::uuid, user_id::bigint
FROM platform.audit_log
WHERE user_id ~ '^[0-9]+$'
ON CONFLICT (user_id) DO NOTHING;
ALTER TABLE platform.audit_log DISABLE RULE no_update_audit_log;
UPDATE platform.audit_log SET caller_role = 'jarvis_migrator' WHERE caller_role IS NULL;
UPDATE platform.audit_log AS event
SET subject_id = subject.id,
    user_id = NULL,
    metadata = event.metadata - ARRAY[
        'ip', 'client_ip', 'raw_client_ip', 'email', 'name', 'username',
        'telegram_username', 'user_agent', 'user_id'
    ]::text[]
FROM platform.audit_subjects AS subject
WHERE event.subject_id IS NULL AND event.user_id = subject.user_id::text;
-- Historical direct identifiers are always erasable.  Values that cannot be
-- mapped to the stable subject table are redacted rather than preserved.
UPDATE platform.audit_log
SET user_id = NULL,
    metadata = metadata - ARRAY[
        'ip', 'client_ip', 'raw_client_ip', 'email', 'name', 'username',
        'telegram_username', 'user_agent', 'user_id'
    ]::text[]
WHERE user_id IS NOT NULL;
ALTER TABLE platform.audit_log ENABLE RULE no_update_audit_log;
ALTER TABLE platform.audit_log ALTER COLUMN caller_role SET NOT NULL;
DO $$
BEGIN
    ALTER TABLE platform.audit_log ADD CONSTRAINT audit_log_caller_role_check CHECK (
        caller_role IN (
            'jarvis_migrator', 'jarvis_platform_runtime',
            'jarvis_research_runtime', 'jarvis_learning_runtime'
        )
    );
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

CREATE OR REPLACE FUNCTION platform.append_audit_event(
    p_user_id text, p_action text, p_resource text, p_metadata jsonb
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog
AS $$
DECLARE
    v_subject_id uuid;
BEGIN
    IF session_user NOT IN (
        'jarvis_platform_runtime', 'jarvis_research_runtime', 'jarvis_learning_runtime'
    ) THEN
        RAISE EXCEPTION 'audit caller is not allowed';
    END IF;
    IF p_action !~ '^[a-z][a-z0-9_.:-]{0,127}$'
       OR p_resource !~ '^/?[a-z][a-z0-9_./:-]{0,255}$'
       OR jsonb_typeof(COALESCE(p_metadata, '{}'::jsonb)) <> 'object'
       OR EXISTS (
           SELECT 1 FROM jsonb_each(COALESCE(p_metadata, '{}'::jsonb)) AS item(key, value)
           WHERE item.key !~ '^[a-z_][a-z0-9_]{0,63}$'
              OR jsonb_typeof(item.value) NOT IN ('boolean', 'number', 'null')
       ) THEN
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
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog
AS $$
DECLARE v_user_id bigint;
BEGIN
    IF session_user <> 'jarvis_erasure_executor' THEN
        RAISE EXCEPTION 'erasure finalizer is executor-only';
    END IF;
    SELECT er.user_id INTO v_user_id
    FROM platform.erasure_requests AS er
    JOIN platform.users AS users ON users.id = er.user_id
    WHERE er.request_id = p_request_id
      AND er.state = 'ready'
      AND er.eligible_at <= NOW()
      AND users.deleted_at IS NOT NULL
      AND users.deleted_at + INTERVAL '30 days' <= NOW()
    FOR UPDATE OF er, users;
    IF v_user_id IS NULL THEN
        RETURN FALSE;
    END IF;
    UPDATE platform.erasure_requests SET state = 'executing'
    WHERE request_id = p_request_id AND state = 'ready';
    IF (SELECT count(*) FROM platform.erasure_acknowledgements
        WHERE request_id = p_request_id AND domain IN ('qdrant', 'research', 'learning')) <> 3
       OR COALESCE((SELECT (receipt->>'residual_points')::int
                    FROM platform.erasure_acknowledgements
                    WHERE request_id = p_request_id AND domain = 'qdrant'), -1) <> 0 THEN
        RAISE EXCEPTION 'erasure acknowledgements are incomplete';
    END IF;
    DELETE FROM platform.audit_subjects WHERE user_id = v_user_id;
    DELETE FROM platform.users WHERE id = v_user_id AND deleted_at IS NOT NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'erasure account is no longer disabled';
    END IF;
    UPDATE platform.erasure_requests SET state = 'complete', completed_at = NOW()
    WHERE request_id = p_request_id AND state = 'executing';
    RETURN TRUE;
END;
$$;
REVOKE ALL ON FUNCTION platform.finalize_erasure(uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA platform TO jarvis_erasure_executor;
GRANT EXECUTE ON FUNCTION platform.finalize_erasure(uuid) TO jarvis_erasure_executor;

RESET ROLE;
