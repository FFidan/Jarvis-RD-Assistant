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
DECLARE
    owner_record ops.job_owner_registry%ROWTYPE;
    runtime_owner text;
BEGIN
    IF NOT (NEW.args ? 'job_id') THEN RETURN NEW; END IF;
    SELECT * INTO owner_record FROM ops.job_owner_registry WHERE task_name = NEW.task_name;
    IF NOT FOUND OR NEW.queue_name <> owner_record.queue_name THEN
        RAISE EXCEPTION 'job queue does not match task owner';
    END IF;
    runtime_owner := CASE session_user
        WHEN 'jarvis_research_runtime' THEN 'research'
        WHEN 'jarvis_learning_runtime' THEN 'learning'
        ELSE NULL END;
    IF runtime_owner IS NOT NULL AND owner_record.service_name <> runtime_owner THEN
        RAISE EXCEPTION 'job task is owned by another runtime';
    END IF;
    IF (NEW.owner_queue IS NOT NULL AND NEW.owner_queue <> owner_record.queue_name)
       OR (NEW.owner_service IS NOT NULL AND NEW.owner_service <> owner_record.service_name) THEN
        RAISE EXCEPTION 'job owner metadata does not match task owner';
    END IF;
    NEW.owner_queue := owner_record.queue_name;
    NEW.owner_service := owner_record.service_name;
    RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION ops.enforce_job_owner_metadata_v1() FROM PUBLIC;

ALTER TABLE ops.procrastinate_jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS research_job_rows_v1 ON ops.procrastinate_jobs;
CREATE POLICY research_job_rows_v1 ON ops.procrastinate_jobs
    FOR ALL TO jarvis_research_runtime
    USING (owner_service = 'research'
        OR (owner_service IS NULL AND queue_name = 'paper_ingestion'))
    WITH CHECK (owner_service = 'research'
        OR (owner_service IS NULL AND queue_name = 'paper_ingestion'));
DROP POLICY IF EXISTS learning_job_rows_v1 ON ops.procrastinate_jobs;
CREATE POLICY learning_job_rows_v1 ON ops.procrastinate_jobs
    FOR ALL TO jarvis_learning_runtime
    USING (owner_service = 'learning'
        OR (owner_service IS NULL AND queue_name = 'learning_engine'))
    WITH CHECK (owner_service = 'learning'
        OR (owner_service IS NULL AND queue_name = 'learning_engine'));

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
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END; $$;
REVOKE ALL ON FUNCTION ops.redact_erased_user_jobs_v1(bigint) FROM PUBLIC;
GRANT USAGE ON SCHEMA ops TO jarvis_platform_owner;
GRANT EXECUTE ON FUNCTION ops.redact_erased_user_jobs_v1(bigint)
    TO jarvis_platform_owner;

RESET ROLE;
SET LOCAL ROLE jarvis_platform_owner;
CREATE OR REPLACE FUNCTION platform.finalize_erasure(p_request_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = platform, pg_catalog AS $$
DECLARE v_user_id bigint;
BEGIN
    IF session_user <> 'jarvis_erasure_executor' THEN
        RAISE EXCEPTION 'erasure finalizer is executor-only';
    END IF;
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
    IF (SELECT count(*) FROM platform.erasure_acknowledgements
        WHERE request_id = p_request_id AND domain IN ('qdrant', 'research', 'learning')) <> 3
       OR COALESCE((SELECT (receipt->>'residual_points')::int
                    FROM platform.erasure_acknowledgements
                    WHERE request_id = p_request_id AND domain = 'qdrant'), -1) <> 0 THEN
        RAISE EXCEPTION 'erasure acknowledgements are incomplete';
    END IF;
    PERFORM ops.redact_erased_user_jobs_v1(v_user_id);
    DELETE FROM platform.config_deliveries WHERE scope_user_id = v_user_id;
    UPDATE platform.config_deliveries
    SET actor_user_id = NULL, state = 'failed', attempts = 8,
        last_error = 'authorizing account erased', updated_at = NOW()
    WHERE actor_user_id = v_user_id;
    DELETE FROM platform.audit_subjects WHERE user_id = v_user_id;
    DELETE FROM platform.users WHERE id = v_user_id AND deleted_at IS NOT NULL;
    IF NOT FOUND THEN RAISE EXCEPTION 'erasure account is no longer disabled'; END IF;
    UPDATE platform.erasure_requests SET state = 'complete', completed_at = NOW()
    WHERE request_id = p_request_id AND state = 'executing';
    RETURN TRUE;
END; $$;
REVOKE ALL ON FUNCTION platform.finalize_erasure(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.finalize_erasure(uuid) TO jarvis_erasure_executor;

RESET ROLE;
SET LOCAL search_path TO ops, public, pg_catalog;
