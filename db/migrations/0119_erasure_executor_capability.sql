-- Restrict the erasure executor to exact, owner-defined capabilities.

SET LOCAL ROLE jarvis_platform_owner;

CREATE OR REPLACE FUNCTION platform.due_erasure_request_ids(p_limit integer)
RETURNS TABLE(request_id uuid)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = platform, pg_catalog
AS $$
BEGIN
    IF session_user <> 'jarvis_erasure_executor'
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'erasure due-request listing is not allowed';
    END IF;

    RETURN QUERY
    SELECT request.request_id
    FROM platform.erasure_requests AS request
    JOIN platform.users AS account ON account.id = request.user_id
    WHERE request.state = 'ready'
      AND request.eligible_at <= NOW()
      AND account.deleted_at IS NOT NULL
      AND account.deleted_at + INTERVAL '30 days' <= NOW()
    ORDER BY request.eligible_at, request.request_id
    LIMIT p_limit;
END;
$$;

REVOKE ALL ON FUNCTION platform.due_erasure_request_ids(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.due_erasure_request_ids(integer)
    TO jarvis_erasure_executor;

CREATE OR REPLACE FUNCTION platform.rotate_visibility_checkpoint_v1(
    p_key text, p_generation text, p_recovery text, p_rotated_at text
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime'
       OR p_key <> 'vector_visibility.checkpoint'
       OR p_generation !~ '^[a-f0-9]{32}$' OR length(p_recovery) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'visibility checkpoint rotation is not allowed';
    END IF;
    RETURN QUERY INSERT INTO platform.user_config(user_id, key, value)
    VALUES (NULL, p_key, jsonb_build_object(
        'version', 1, 'visibility_generation', p_generation, 'status', 'pending',
        'last_chunk_id', 0, 'qdrant_recovery', p_recovery, 'rotated_at', p_rotated_at))
    ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    RETURNING user_config.value;
END;
$$;

CREATE OR REPLACE FUNCTION platform.claim_visibility_lease_v1(
    p_key text, p_generation text, p_worker text, p_seconds integer
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_key <> 'vector_visibility.checkpoint'
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
END;
$$;

CREATE OR REPLACE FUNCTION platform.advance_visibility_checkpoint_v1(
    p_key text, p_generation text, p_worker text, p_chunk bigint, p_seconds integer
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_key <> 'vector_visibility.checkpoint'
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
END;
$$;

CREATE OR REPLACE FUNCTION platform.complete_visibility_checkpoint_v1(
    p_key text, p_generation text, p_worker text
) RETURNS TABLE(value jsonb) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = platform, pg_catalog AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_key <> 'vector_visibility.checkpoint' THEN
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
END;
$$;

RESET ROLE;
