-- Move erasure state changes behind owner-defined capabilities so the platform
-- runtime cannot bypass the finalization invariants with direct table writes.

SET LOCAL ROLE jarvis_platform_owner;

CREATE OR REPLACE FUNCTION platform.request_erasure_v1(p_user_id bigint)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    existing uuid;
    created uuid;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' OR p_user_id IS NULL THEN
        RAISE EXCEPTION 'erasure request is not allowed';
    END IF;
    SELECT request_id INTO existing
    FROM platform.erasure_requests
    WHERE user_id = p_user_id AND state NOT IN ('complete', 'attention_required')
    ORDER BY requested_at DESC
    LIMIT 1;
    IF existing IS NOT NULL THEN
        RETURN existing;
    END IF;
    -- eligible_at is derived here, never supplied by the caller, so the
    -- restore grace cannot be shortened by the service that requests erasure.
    INSERT INTO platform.erasure_requests
        (request_id, user_id, state, resume_state, eligible_at)
    SELECT gen_random_uuid(), account.id, 'requested', 'qdrant_pending',
           account.deleted_at + INTERVAL '30 days'
    FROM platform.users AS account
    WHERE account.id = p_user_id AND account.deleted_at IS NOT NULL
    RETURNING request_id INTO created;
    RETURN created;
END;
$$;

CREATE OR REPLACE FUNCTION platform.begin_erasure_destructive_v1(p_request_id uuid)
RETURNS TABLE(state text, attempts integer, resume_state text,
              eligible_at timestamptz, deleted_at timestamptz, user_id bigint)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    request record;
    account record;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' THEN
        RAISE EXCEPTION 'erasure phase change is not allowed';
    END IF;
    SELECT * INTO request FROM platform.erasure_requests
    WHERE request_id = p_request_id FOR UPDATE;
    IF request IS NULL THEN
        RAISE EXCEPTION 'erasure request does not exist';
    END IF;
    SELECT * INTO account FROM platform.users
    WHERE id = request.user_id FOR UPDATE;
    IF account IS NULL OR account.deleted_at IS NULL THEN
        RAISE EXCEPTION 'erasure account is no longer disabled';
    END IF;
    IF request.state = 'requested' THEN
        -- Both clocks must have elapsed. Checking them inside the capability
        -- is what makes the grace boundary independent of the caller.
        IF NOT (request.eligible_at <= NOW()
                AND account.deleted_at + INTERVAL '30 days' <= NOW()) THEN
            RAISE EXCEPTION 'account erasure is still inside the restore grace';
        END IF;
        UPDATE platform.erasure_requests AS pending
        SET state = 'qdrant_pending'
        WHERE pending.request_id = p_request_id AND pending.state = 'requested';
        request.state := 'qdrant_pending';
    END IF;
    RETURN QUERY SELECT request.state, request.attempts, request.resume_state,
                        request.eligible_at, account.deleted_at, request.user_id;
END;
$$;

CREATE OR REPLACE FUNCTION platform.record_erasure_ack_v1(
    p_request_id uuid, p_domain text, p_receipt jsonb
)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    phase_order constant text[] := ARRAY['qdrant_pending', 'research_pending',
                                         'learning_pending', 'ready', 'executing'];
    reached text;
BEGIN
    IF session_user <> 'jarvis_platform_runtime'
       OR p_domain NOT IN ('qdrant', 'research', 'learning') THEN
        RAISE EXCEPTION 'erasure acknowledgement is not allowed';
    END IF;
    -- A receipt binds to the phase that produced it. One for a phase this
    -- request has not reached would let the caller assemble finalization's
    -- preconditions before the owning service ran, while one for a phase
    -- already passed is the legitimate replay of durable work. A waiting
    -- request is judged by the phase it will resume, and a finalized request is
    -- history no acknowledgement may amend.
    SELECT CASE WHEN request.state IN ('retry_wait', 'attention_required')
                THEN request.resume_state ELSE request.state END
      INTO reached
    FROM platform.erasure_requests AS request
    WHERE request.request_id = p_request_id AND request.state <> 'complete';
    IF COALESCE(array_position(phase_order, reached), 0)
       < array_position(phase_order, p_domain || '_pending') THEN
        RAISE EXCEPTION 'erasure acknowledgement is not allowed for this request';
    END IF;
    INSERT INTO platform.erasure_acknowledgements (request_id, domain, receipt)
    VALUES (p_request_id, p_domain, p_receipt)
    ON CONFLICT (request_id, domain)
    DO UPDATE SET receipt = EXCLUDED.receipt, acknowledged_at = NOW();
END;
$$;

-- The account-erasure state graph exactly as db/ownership-manifest.json
-- declares it, minus every edge into 'complete': that step belongs to the
-- executor's finalization capability and no runtime capability may reach it.
-- Declaring the graph here is what makes it a boundary rather than advice --
-- the platform runtime is untrusted for erasure state.
CREATE OR REPLACE FUNCTION platform.erasure_transition_allowed_v1(
    p_source text, p_target text
)
RETURNS boolean
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM (VALUES
            ('requested', 'qdrant_pending'),
            ('qdrant_pending', 'research_pending'),
            ('qdrant_pending', 'retry_wait'),
            ('qdrant_pending', 'attention_required'),
            ('research_pending', 'learning_pending'),
            ('research_pending', 'retry_wait'),
            ('research_pending', 'attention_required'),
            ('learning_pending', 'ready'),
            ('learning_pending', 'retry_wait'),
            ('learning_pending', 'attention_required'),
            ('ready', 'executing'),
            ('executing', 'retry_wait'),
            ('executing', 'attention_required'),
            ('retry_wait', 'qdrant_pending'),
            ('retry_wait', 'research_pending'),
            ('retry_wait', 'learning_pending'),
            ('retry_wait', 'attention_required'),
            ('attention_required', 'qdrant_pending'),
            ('attention_required', 'research_pending'),
            ('attention_required', 'learning_pending')
        ) AS edge(source, target)
        WHERE edge.source = p_source AND edge.target = p_target
    );
$$;
REVOKE ALL ON FUNCTION platform.erasure_transition_allowed_v1(text,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION platform.transition_erasure_v1(
    p_request_id uuid, p_target text, p_expected text
)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    persisted text;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' THEN
        RAISE EXCEPTION 'erasure transition is not allowed';
    END IF;
    -- 'complete' is reached only by the executor's finalization capability.
    IF p_target = 'complete' THEN
        RAISE EXCEPTION 'erasure completion is reserved for finalization';
    END IF;
    IF NOT platform.erasure_transition_allowed_v1(p_expected, p_target) THEN
        RAISE EXCEPTION 'erasure transition % -> % is refused by the state graph',
                        p_expected, p_target;
    END IF;
    -- last_error is the only durable record of why a request stranded, so the
    -- state it strands in keeps it. Every other target has recovered from
    -- whatever that error described.
    UPDATE platform.erasure_requests AS request
    SET state = p_target,
        last_error = CASE WHEN p_target = 'attention_required'
                          THEN request.last_error ELSE NULL END,
        next_attempt_at = CASE WHEN p_target = 'retry_wait'
                               THEN request.next_attempt_at ELSE NOW() END
    WHERE request.request_id = p_request_id AND request.state = p_expected
    RETURNING request.state INTO persisted;
    RETURN persisted;
END;
$$;

CREATE OR REPLACE FUNCTION platform.record_erasure_retry_v1(
    p_request_id uuid, p_resume_state text
)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    persisted text;
BEGIN
    IF session_user <> 'jarvis_platform_runtime'
       OR p_resume_state NOT IN ('qdrant_pending', 'research_pending',
                                 'learning_pending') THEN
        RAISE EXCEPTION 'erasure retry is not allowed';
    END IF;
    UPDATE platform.erasure_requests AS request
    SET attempts = request.attempts + 1,
        state = CASE WHEN request.attempts + 1 >= 8
                     THEN 'attention_required' ELSE 'retry_wait' END,
        resume_state = p_resume_state,
        last_error = 'owner command unavailable',
        next_attempt_at = NOW() + LEAST(
            INTERVAL '1 hour', POWER(2, request.attempts) * INTERVAL '30 seconds'
        )
    WHERE request.request_id = p_request_id
      AND request.state = p_resume_state
      AND request.attempts < 8
    RETURNING request.state INTO persisted;
    RETURN persisted;
END;
$$;

CREATE OR REPLACE FUNCTION platform.resume_erasure_v1(p_request_id uuid)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    request record;
    account record;
    resumed text;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' THEN
        RAISE EXCEPTION 'erasure resume is not allowed';
    END IF;
    SELECT * INTO request FROM platform.erasure_requests
    WHERE request_id = p_request_id FOR UPDATE;
    IF request IS NULL THEN
        RAISE EXCEPTION 'erasure request does not exist';
    END IF;
    IF request.state <> 'attention_required' THEN
        RAISE EXCEPTION 'erasure resume needs a stranded request';
    END IF;
    IF NOT platform.erasure_transition_allowed_v1(request.state, request.resume_state) THEN
        RAISE EXCEPTION 'erasure resume % -> % is refused by the state graph',
                        request.state, request.resume_state;
    END IF;
    -- platform_erasure_requests_one_active_user_idx excludes stranded requests
    -- on purpose, so a new erasure can be requested past one. Resuming into
    -- that window would violate the index, so the stranded request waits.
    IF EXISTS (
        SELECT 1 FROM platform.erasure_requests AS newer
        WHERE newer.user_id = request.user_id
          AND newer.request_id <> p_request_id
          AND newer.state NOT IN ('complete', 'attention_required')
    ) THEN
        RAISE EXCEPTION 'erasure resume is superseded by a newer request';
    END IF;
    SELECT * INTO account FROM platform.users
    WHERE id = request.user_id FOR UPDATE;
    IF account IS NULL OR account.deleted_at IS NULL THEN
        RAISE EXCEPTION 'erasure account is no longer disabled';
    END IF;
    -- The attempt counter resets with the state. record_erasure_retry_v1 only
    -- matches below its ceiling, so a resume that left the counter exhausted
    -- would strand the request again on its first failure, with no retry and
    -- no recorded reason.
    UPDATE platform.erasure_requests AS stranded
    SET state = stranded.resume_state, attempts = 0, last_error = NULL,
        next_attempt_at = NOW()
    WHERE stranded.request_id = p_request_id
      AND stranded.state = 'attention_required'
    RETURNING stranded.state INTO resumed;
    RETURN resumed;
END;
$$;

CREATE OR REPLACE FUNCTION platform.set_account_deleted_v1(p_user_id bigint)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    changed boolean;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' OR p_user_id IS NULL THEN
        RAISE EXCEPTION 'account deletion is not allowed';
    END IF;
    -- NOW() is fixed here, so the grace window cannot start in the past.
    UPDATE platform.users SET deleted_at = NOW()
    WHERE id = p_user_id AND deleted_at IS NULL
    RETURNING true INTO changed;
    RETURN COALESCE(changed, false);
END;
$$;

CREATE OR REPLACE FUNCTION platform.restore_account_v1(p_user_id bigint)
RETURNS TABLE(id bigint, email text, role text,
              created_at timestamptz, last_login_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
DECLARE
    started text;
BEGIN
    IF session_user <> 'jarvis_platform_runtime' OR p_user_id IS NULL THEN
        RAISE EXCEPTION 'account restore is not allowed';
    END IF;
    -- Every erasure capability locks platform.erasure_requests before
    -- platform.users: this restore races the destructive start by design, and
    -- opposite orders deadlock. Locking all of this account's unfinished
    -- requests covers exactly the rows the DELETE below removes.
    PERFORM 1 FROM platform.erasure_requests AS request
    WHERE request.user_id = p_user_id AND request.state <> 'complete'
    ORDER BY request.request_id
    FOR UPDATE;
    PERFORM 1 FROM platform.users AS account
    WHERE account.id = p_user_id AND account.deleted_at IS NOT NULL
    FOR UPDATE;
    -- Restoring an account whose erasure has passed the request phase would
    -- resurrect a user whose domain data is already being removed, so the
    -- refusal is decided here under the same lock that performs the restore.
    -- 'attention_required' refuses too: it is reached only after every
    -- destructive attempt failed, with domain data already gone.
    SELECT request.state INTO started
    FROM platform.erasure_requests AS request
    WHERE request.user_id = p_user_id
      AND request.state NOT IN ('complete', 'requested')
    LIMIT 1;
    IF started IS NOT NULL THEN
        RAISE EXCEPTION 'account erasure has already started';
    END IF;
    DELETE FROM platform.erasure_requests AS request
    WHERE request.user_id = p_user_id AND request.state <> 'complete';
    RETURN QUERY
    UPDATE platform.users AS account SET deleted_at = NULL
    WHERE account.id = p_user_id
      AND account.deleted_at IS NOT NULL
      AND account.deleted_at >= NOW() - INTERVAL '30 days'
    RETURNING account.id, account.email, account.role,
              account.created_at, account.last_login_at;
END;
$$;

REVOKE ALL ON FUNCTION
    platform.request_erasure_v1(bigint),
    platform.begin_erasure_destructive_v1(uuid),
    platform.record_erasure_ack_v1(uuid,text,jsonb),
    platform.transition_erasure_v1(uuid,text,text),
    platform.record_erasure_retry_v1(uuid,text),
    platform.resume_erasure_v1(uuid),
    platform.set_account_deleted_v1(bigint),
    platform.restore_account_v1(bigint)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    platform.request_erasure_v1(bigint),
    platform.begin_erasure_destructive_v1(uuid),
    platform.record_erasure_ack_v1(uuid,text,jsonb),
    platform.transition_erasure_v1(uuid,text,text),
    platform.record_erasure_retry_v1(uuid,text),
    platform.resume_erasure_v1(uuid),
    platform.set_account_deleted_v1(bigint),
    platform.restore_account_v1(bigint)
TO jarvis_platform_runtime;

-- With every legitimate write available as a capability, direct writes to the
-- erasure tables are withdrawn. The runtime keeps column-scoped UPDATE on
-- accounts for profile and role administration, but no longer controls the
-- deletion clock the grace window is measured from, and can no longer remove
-- an account outside finalization.
REVOKE INSERT, UPDATE, DELETE ON platform.erasure_requests FROM jarvis_platform_runtime;
REVOKE INSERT, UPDATE, DELETE ON platform.erasure_acknowledgements FROM jarvis_platform_runtime;
REVOKE UPDATE, DELETE ON platform.users FROM jarvis_platform_runtime;
GRANT UPDATE (email, role, display_name, last_login_at)
    ON platform.users TO jarvis_platform_runtime;

RESET ROLE;
