-- Enforce the final cross-domain runtime privilege boundary.
--
-- Owner-local runtimes retain full DML in their own schemas. Cross-domain
-- reads stay relation-specific, mutations use the capabilities installed by
-- 0115-0117, and the shared Operations queue keeps its RLS-protected worker
-- tables while job progress remains capability-only.

SET LOCAL ROLE jarvis_platform_owner;

CREATE OR REPLACE FUNCTION platform.audit_readiness_v1()
RETURNS TABLE(latest_event_at timestamptz, event_count bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = platform, pg_catalog
AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' THEN
        RAISE EXCEPTION 'audit readiness caller is not allowed';
    END IF;
    RETURN QUERY
    SELECT MAX(audit_log."timestamp"), COUNT(*)
    FROM platform.audit_log;
END;
$$;

REVOKE ALL ON FUNCTION platform.audit_readiness_v1()
FROM PUBLIC, jarvis_platform_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION platform.audit_readiness_v1() TO jarvis_research_runtime;

-- Runtime roles receive table and sequence defaults for owner-local persistence,
-- but every callable capability is granted by its defining migration. A future
-- SECURITY DEFINER helper must never become executable merely because its owner
-- created it in the service schema.
ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_platform_owner IN SCHEMA platform
REVOKE EXECUTE ON FUNCTIONS FROM jarvis_platform_runtime;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;
SET LOCAL ROLE jarvis_research_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_research_owner IN SCHEMA research
REVOKE EXECUTE ON FUNCTIONS FROM jarvis_research_runtime;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;
SET LOCAL ROLE jarvis_learning_owner;

CREATE OR REPLACE FUNCTION learning.update_scheduled_nudge_v1(
    p_nudge_id integer,
    p_set_cron boolean,
    p_cron_expression text,
    p_set_enabled boolean,
    p_enabled boolean,
    p_set_config boolean,
    p_config jsonb
)
RETURNS SETOF learning.scheduled_nudges
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = learning, pg_catalog
AS $$
BEGIN
    IF session_user <> 'jarvis_research_runtime' OR p_nudge_id <= 0 THEN
        RAISE EXCEPTION 'scheduled nudge caller is not allowed';
    END IF;
    RETURN QUERY
    UPDATE learning.scheduled_nudges AS nudge
    SET cron_expression = CASE
            WHEN p_set_cron THEN p_cron_expression
            ELSE nudge.cron_expression
        END,
        enabled = CASE WHEN p_set_enabled THEN p_enabled ELSE nudge.enabled END,
        config = CASE WHEN p_set_config THEN p_config ELSE nudge.config END
    WHERE nudge.id = p_nudge_id
    RETURNING nudge.*;
END;
$$;

REVOKE ALL ON FUNCTION learning.update_scheduled_nudge_v1(
    integer,boolean,text,boolean,boolean,boolean,jsonb
) FROM PUBLIC, jarvis_platform_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION learning.update_scheduled_nudge_v1(
    integer,boolean,text,boolean,boolean,boolean,jsonb
) TO jarvis_research_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_learning_owner IN SCHEMA learning
REVOKE EXECUTE ON FUNCTIONS FROM jarvis_learning_runtime;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;
SET LOCAL ROLE jarvis_platform_owner;

REVOKE EXECUTE ON FUNCTION
    platform.set_research_config_v1(bigint,text,jsonb,text),
    platform.purge_identity_retention_v1(text),
    platform.purge_system_events_v1(text),
    platform.rotate_visibility_checkpoint_v1(text,text,text,text),
    platform.claim_visibility_lease_v1(text,text,text,integer),
    platform.advance_visibility_checkpoint_v1(text,text,text,bigint,integer),
    platform.complete_visibility_checkpoint_v1(text,text,text)
FROM jarvis_platform_runtime, jarvis_learning_runtime;
REVOKE EXECUTE ON FUNCTION platform.finalize_erasure(uuid)
FROM jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;

REVOKE SELECT, INSERT, UPDATE, DELETE ON
    platform.audit_log,
    platform.magic_link_tokens,
    platform.sessions,
    platform.system_events,
    platform.user_config,
    platform.users,
    platform.webauthn_challenges
FROM jarvis_research_runtime;
GRANT SELECT ON platform.user_config, platform.users TO jarvis_research_runtime;

REVOKE SELECT, INSERT, UPDATE, DELETE ON
    platform.llm_usage_log,
    platform.user_config,
    platform.users
FROM jarvis_learning_runtime;
GRANT SELECT ON platform.llm_usage_log, platform.user_config TO jarvis_learning_runtime;

REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA platform
FROM jarvis_research_runtime, jarvis_learning_runtime;

RESET ROLE;
SET LOCAL ROLE jarvis_learning_owner;

REVOKE EXECUTE ON FUNCTION learning.clear_zotero_collection_keys_v1(integer)
FROM jarvis_learning_runtime;

REVOKE SELECT, INSERT, UPDATE, DELETE ON
    learning.cards,
    learning.daily_log,
    learning.decks,
    learning.journal_entries,
    learning.milestones,
    learning.project_papers,
    learning.projects,
    learning.review_logs,
    learning.scheduled_nudges,
    learning.task_paper_links,
    learning.tasks
FROM jarvis_research_runtime;
GRANT SELECT ON
    learning.cards,
    learning.daily_log,
    learning.decks,
    learning.journal_entries,
    learning.milestones,
    learning.project_papers,
    learning.projects,
    learning.review_logs,
    learning.scheduled_nudges,
    learning.task_paper_links,
    learning.tasks
TO jarvis_research_runtime;
REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA learning FROM jarvis_research_runtime;

RESET ROLE;
SET LOCAL ROLE jarvis_research_owner;

REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research FROM jarvis_learning_runtime;

RESET ROLE;
SET LOCAL ROLE jarvis_ops_owner;

REVOKE SELECT, INSERT, UPDATE, DELETE ON ops.job_progress
FROM jarvis_research_runtime, jarvis_learning_runtime;
GRANT SELECT ON ops.schema_migrations TO jarvis_platform_runtime;

RESET ROLE;
