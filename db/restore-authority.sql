-- Reconstruct v1.2.6 database authority after an owner/ACL-free restore.
--
-- This file is intentionally separate from the forward migration ledger. A
-- current backup already contains every migration marker, while pg_dump omits
-- the role-bound authority that must be rebuilt from the destination install.

\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    live_floor integer;
    domain record;
BEGIN
    SELECT COALESCE(MAX(version), 0) INTO live_floor FROM ops.schema_migrations;
    IF live_floor <> 120 THEN
        RAISE EXCEPTION 'restore authority requires packaged schema 120, found %', live_floor;
    END IF;

    ALTER ROLE jarvis_backup_reader WITH BYPASSRLS;
    ALTER ROLE jarvis_restore_operator WITH BYPASSRLS;
    EXECUTE format('ALTER DATABASE %I OWNER TO jarvis_legacy_rollback', current_database());
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO jarvis_platform_runtime, jarvis_research_runtime, '
        'jarvis_learning_runtime, jarvis_migrator, jarvis_legacy_rollback, '
        'jarvis_backup_reader, jarvis_restore_operator, jarvis_erasure_executor',
        current_database()
    );

    REVOKE CREATE ON SCHEMA public FROM PUBLIC;

    FOR domain IN
        SELECT * FROM (
            VALUES
                ('platform', 'jarvis_platform_owner', 'jarvis_platform_runtime'),
                ('research', 'jarvis_research_owner', 'jarvis_research_runtime'),
                ('learning', 'jarvis_learning_owner', 'jarvis_learning_runtime'),
                ('ops', 'jarvis_ops_owner', NULL::text)
        ) AS domains(schema_name, owner_role, runtime_role)
    LOOP
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format(
            'GRANT USAGE ON SCHEMA %I TO jarvis_legacy_rollback, jarvis_backup_reader, jarvis_restore_operator',
            domain.schema_name
        );
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO jarvis_legacy_rollback',
            domain.schema_name
        );
        EXECUTE format(
            'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO jarvis_legacy_rollback',
            domain.schema_name
        );
        EXECUTE format(
            'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO jarvis_legacy_rollback',
            domain.schema_name
        );
        EXECUTE format(
            'GRANT SELECT ON ALL TABLES IN SCHEMA %I TO jarvis_backup_reader, jarvis_restore_operator',
            domain.schema_name
        );
        EXECUTE format(
            'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO jarvis_backup_reader, jarvis_restore_operator',
            domain.schema_name
        );
        IF domain.runtime_role IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', domain.schema_name, domain.runtime_role);
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
                domain.schema_name, domain.runtime_role
            );
            EXECUTE format(
                'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I',
                domain.schema_name, domain.runtime_role
            );
            EXECUTE format(
                'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I',
                domain.schema_name, domain.runtime_role
            );
        END IF;

        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON TABLES FROM PUBLIC',
            domain.owner_role, domain.schema_name
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM PUBLIC',
            domain.owner_role, domain.schema_name
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE USAGE ON TYPES FROM PUBLIC',
            domain.owner_role, domain.schema_name
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO jarvis_backup_reader, jarvis_restore_operator',
            domain.owner_role, domain.schema_name
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO jarvis_backup_reader, jarvis_restore_operator',
            domain.owner_role, domain.schema_name
        );
        IF domain.runtime_role IS NOT NULL THEN
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
                domain.owner_role, domain.schema_name, domain.runtime_role
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO %I',
                domain.owner_role, domain.schema_name, domain.runtime_role
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE EXECUTE ON FUNCTIONS FROM %I',
                domain.owner_role, domain.schema_name, domain.runtime_role
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE ON TYPES TO %I',
                domain.owner_role, domain.schema_name, domain.runtime_role
            );
        END IF;
    END LOOP;
END $$;

-- Keep these explicit rather than relying on dynamic default-ACL statements:
-- PostgreSQL's built-in function default grants EXECUTE to PUBLIC when no ACL
-- row exists, so each owner must retain an owner-only function default.
SET LOCAL ROLE jarvis_platform_owner;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA platform
REVOKE EXECUTE ON FUNCTIONS FROM jarvis_platform_runtime;
RESET ROLE;
SET LOCAL ROLE jarvis_research_owner;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA research
REVOKE EXECUTE ON FUNCTIONS FROM jarvis_research_runtime;
RESET ROLE;
SET LOCAL ROLE jarvis_learning_owner;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA learning
REVOKE EXECUTE ON FUNCTIONS FROM jarvis_learning_runtime;
RESET ROLE;
SET LOCAL ROLE jarvis_ops_owner;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;

GRANT USAGE ON SCHEMA platform TO
    jarvis_research_runtime, jarvis_learning_runtime, jarvis_erasure_executor;
GRANT SELECT ON platform.user_config, platform.users TO jarvis_research_runtime;
GRANT SELECT ON platform.llm_usage_log, platform.user_config TO jarvis_learning_runtime;

GRANT USAGE ON SCHEMA research TO jarvis_learning_runtime;
GRANT SELECT ON
    research.paper_chunks,
    research.paper_recommendations,
    research.paper_summaries,
    research.paper_user_state,
    research.paper_user_zotero_links,
    research.papers,
    research.thread,
    research.user_library
TO jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION research.upsert_paper_state_v1(integer,integer,text,boolean,text)
TO jarvis_learning_runtime;

GRANT USAGE ON SCHEMA learning TO jarvis_research_runtime;
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

GRANT USAGE ON SCHEMA ops TO
    jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime, jarvis_migrator;
GRANT SELECT ON ops.schema_migrations TO
    jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
GRANT SELECT, INSERT ON ops.schema_migrations TO jarvis_migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    ops.procrastinate_events,
    ops.procrastinate_jobs,
    ops.procrastinate_periodic_defers,
    ops.procrastinate_workers
TO jarvis_research_runtime, jarvis_learning_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO
    jarvis_research_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ops TO
    jarvis_research_runtime, jarvis_learning_runtime;

REVOKE ALL ON FUNCTION
    platform.upsert_config_v1(bigint,text,jsonb,bytea),
    platform.reencrypt_config_v1(integer,bytea),
    platform.set_research_config_v1(bigint,text,jsonb,text),
    platform.append_system_event_v1(text,text,text,text,jsonb,uuid),
    platform.mint_session_v1(bigint,timestamptz,uuid),
    platform.renew_session_v1(text,interval,interval),
    platform.purge_identity_retention_v1(text),
    platform.purge_system_events_v1(text),
    platform.rotate_visibility_checkpoint_v1(text,text,text,text),
    platform.claim_visibility_lease_v1(text,text,text,integer),
    platform.advance_visibility_checkpoint_v1(text,text,text,bigint,integer),
    platform.complete_visibility_checkpoint_v1(text,text,text),
    platform.append_audit_event(text,text,text,jsonb),
    platform.due_erasure_request_ids(integer),
    platform.finalize_erasure(uuid),
    platform.audit_readiness_v1()
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    platform.upsert_config_v1(bigint,text,jsonb,bytea),
    platform.reencrypt_config_v1(integer,bytea),
    platform.mint_session_v1(bigint,timestamptz,uuid),
    platform.renew_session_v1(text,interval,interval)
TO jarvis_platform_runtime;
GRANT EXECUTE ON FUNCTION
    platform.set_research_config_v1(bigint,text,jsonb,text),
    platform.purge_identity_retention_v1(text),
    platform.purge_system_events_v1(text),
    platform.rotate_visibility_checkpoint_v1(text,text,text,text),
    platform.claim_visibility_lease_v1(text,text,text,integer),
    platform.advance_visibility_checkpoint_v1(text,text,text,bigint,integer),
    platform.complete_visibility_checkpoint_v1(text,text,text)
TO jarvis_research_runtime;
GRANT EXECUTE ON FUNCTION platform.append_system_event_v1(text,text,text,text,jsonb,uuid)
TO jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION platform.append_audit_event(text,text,text,jsonb)
TO jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION platform.audit_readiness_v1() TO jarvis_research_runtime;
GRANT EXECUTE ON FUNCTION
    platform.due_erasure_request_ids(integer),
    platform.finalize_erasure(uuid)
TO jarvis_erasure_executor;
REVOKE EXECUTE ON FUNCTION
    platform.set_research_config_v1(bigint,text,jsonb,text),
    platform.purge_identity_retention_v1(text),
    platform.purge_system_events_v1(text),
    platform.rotate_visibility_checkpoint_v1(text,text,text,text),
    platform.claim_visibility_lease_v1(text,text,text,integer),
    platform.advance_visibility_checkpoint_v1(text,text,text,bigint,integer),
    platform.complete_visibility_checkpoint_v1(text,text,text),
    platform.due_erasure_request_ids(integer),
    platform.finalize_erasure(uuid),
    platform.audit_readiness_v1()
FROM jarvis_platform_runtime;
REVOKE EXECUTE ON FUNCTION
    platform.due_erasure_request_ids(integer),
    platform.finalize_erasure(uuid)
FROM jarvis_research_runtime, jarvis_learning_runtime;

REVOKE ALL ON FUNCTION learning.clear_zotero_collection_keys_v1(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION learning.clear_zotero_collection_keys_v1(integer)
TO jarvis_research_runtime;
REVOKE EXECUTE ON FUNCTION learning.clear_zotero_collection_keys_v1(integer)
FROM jarvis_learning_runtime;

REVOKE ALL ON FUNCTION learning.update_scheduled_nudge_v1(
    integer,boolean,text,boolean,boolean,boolean,jsonb
) FROM PUBLIC, jarvis_platform_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION learning.update_scheduled_nudge_v1(
    integer,boolean,text,boolean,boolean,boolean,jsonb
) TO jarvis_research_runtime;

REVOKE ALL ON FUNCTION
    ops.jarvis_job_read_v1(text),
    ops.jarvis_job_list_v1(text,text,text,integer),
    ops.jarvis_job_cancel_v1(text,text),
    ops.record_job_progress_v1(text,real,text),
    ops.record_job_outcome_v1(text,jsonb,jsonb,boolean),
    ops.purge_orphaned_job_progress_v1(),
    ops.redact_erased_user_jobs_v1(bigint)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    ops.jarvis_job_read_v1(text),
    ops.jarvis_job_list_v1(text,text,text,integer),
    ops.jarvis_job_cancel_v1(text,text)
TO jarvis_platform_runtime;
GRANT EXECUTE ON FUNCTION
    ops.record_job_progress_v1(text,real,text),
    ops.record_job_outcome_v1(text,jsonb,jsonb,boolean)
TO jarvis_research_runtime, jarvis_learning_runtime;
GRANT EXECUTE ON FUNCTION ops.purge_orphaned_job_progress_v1()
TO jarvis_research_runtime;
GRANT USAGE ON SCHEMA ops TO jarvis_platform_owner;
GRANT EXECUTE ON FUNCTION ops.redact_erased_user_jobs_v1(bigint)
TO jarvis_platform_owner;

REVOKE SELECT, INSERT, UPDATE, DELETE ON ops.job_progress
FROM jarvis_research_runtime, jarvis_learning_runtime;
REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA platform
FROM jarvis_research_runtime, jarvis_learning_runtime;
REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research FROM jarvis_learning_runtime;
REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA learning FROM jarvis_research_runtime;
-- The ALL-FUNCTIONS and ALL-TABLES grants above are broader than the built
-- system. Narrow them to the fresh-install boundary in db/init.sql so a
-- restored deployment is never weaker than a fresh one: the job facade is
-- Platform-only, and the append-only audit tables take writes solely through
-- platform.append_audit_event, which validates shape and binds the caller.
REVOKE EXECUTE ON FUNCTION
    ops.jarvis_job_read_v1(text),
    ops.jarvis_job_list_v1(text,text,text,integer),
    ops.jarvis_job_cancel_v1(text,text)
FROM jarvis_research_runtime, jarvis_learning_runtime;
REVOKE INSERT, UPDATE, DELETE ON platform.audit_log, platform.audit_subjects
FROM jarvis_platform_runtime;
-- Erasure state changes belong to the owner-defined capabilities, so the
-- platform runtime keeps only column-scoped account administration.
REVOKE ALL ON FUNCTION
    platform.request_erasure_v1(bigint),
    platform.begin_erasure_destructive_v1(uuid),
    platform.record_erasure_ack_v1(uuid,text,jsonb),
    platform.transition_erasure_v1(uuid,text,text),
    platform.record_erasure_retry_v1(uuid,text),
    platform.withdraw_erasure_v1(bigint),
    platform.set_account_deleted_v1(bigint),
    platform.restore_account_v1(bigint)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    platform.request_erasure_v1(bigint),
    platform.begin_erasure_destructive_v1(uuid),
    platform.record_erasure_ack_v1(uuid,text,jsonb),
    platform.transition_erasure_v1(uuid,text,text),
    platform.record_erasure_retry_v1(uuid,text),
    platform.withdraw_erasure_v1(bigint),
    platform.set_account_deleted_v1(bigint),
    platform.restore_account_v1(bigint)
TO jarvis_platform_runtime;
REVOKE INSERT, UPDATE, DELETE ON platform.erasure_requests FROM jarvis_platform_runtime;
REVOKE INSERT, UPDATE, DELETE ON platform.erasure_acknowledgements FROM jarvis_platform_runtime;
REVOKE UPDATE, DELETE ON platform.users FROM jarvis_platform_runtime;
GRANT UPDATE (email, role, display_name, last_login_at)
    ON platform.users TO jarvis_platform_runtime;

ALTER ROLE jarvis_platform_owner SET search_path TO platform, pg_catalog;
ALTER ROLE jarvis_research_owner SET search_path TO research, pg_catalog;
ALTER ROLE jarvis_learning_owner SET search_path TO learning, pg_catalog;
ALTER ROLE jarvis_ops_owner SET search_path TO ops, pg_catalog;
ALTER ROLE jarvis_platform_runtime SET search_path TO platform, ops, public, pg_catalog;
ALTER ROLE jarvis_research_runtime SET search_path TO research, platform, learning, ops, public, pg_catalog;
ALTER ROLE jarvis_learning_runtime SET search_path TO learning, research, platform, ops, public, pg_catalog;
ALTER ROLE jarvis_migrator SET search_path TO ops, platform, research, learning, public, pg_catalog;
ALTER ROLE jarvis_legacy_rollback SET search_path TO platform, research, learning, ops, public, pg_catalog;

DO $$
DECLARE
    domain record;
    invalid_count bigint;
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
        SELECT
            (SELECT count(*) FROM pg_namespace AS namespace
             JOIN pg_roles AS owner ON owner.oid = namespace.nspowner
             WHERE namespace.nspname = domain.schema_name
               AND owner.rolname <> domain.owner_role)
            +
            (SELECT count(*) FROM pg_class AS relation
             JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
             JOIN pg_roles AS owner ON owner.oid = relation.relowner
             WHERE namespace.nspname = domain.schema_name
               AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
               AND owner.rolname <> domain.owner_role
               AND NOT EXISTS (
                   SELECT 1 FROM pg_depend AS dependency
                   WHERE dependency.classid = 'pg_class'::regclass
                     AND dependency.objid = relation.oid
                     AND dependency.deptype = 'e'))
            +
            (SELECT count(*) FROM pg_proc AS function
             JOIN pg_namespace AS namespace ON namespace.oid = function.pronamespace
             JOIN pg_roles AS owner ON owner.oid = function.proowner
             WHERE namespace.nspname = domain.schema_name
               AND owner.rolname <> domain.owner_role
               AND NOT EXISTS (
                   SELECT 1 FROM pg_depend AS dependency
                   WHERE dependency.classid = 'pg_proc'::regclass
                     AND dependency.objid = function.oid
                     AND dependency.deptype = 'e'))
            +
            (SELECT count(*) FROM pg_type AS type
             JOIN pg_namespace AS namespace ON namespace.oid = type.typnamespace
             JOIN pg_roles AS owner ON owner.oid = type.typowner
             WHERE namespace.nspname = domain.schema_name
               AND owner.rolname <> domain.owner_role
               AND ((type.typrelid = 0 AND type.typtype IN ('d', 'e', 'm', 'r'))
                    OR (type.typtype = 'c' AND EXISTS (
                        SELECT 1 FROM pg_class AS composite_relation
                        WHERE composite_relation.oid = type.typrelid
                          AND composite_relation.relkind = 'c')))
               AND NOT EXISTS (
                   SELECT 1 FROM pg_depend AS dependency
                   WHERE dependency.classid = 'pg_type'::regclass
                     AND dependency.objid = type.oid
                     AND dependency.deptype = 'e'))
        INTO invalid_count;
        IF invalid_count <> 0 THEN
            RAISE EXCEPTION 'restored schema % has % objects with invalid ownership',
                domain.schema_name, invalid_count;
        END IF;
    END LOOP;
END $$;

COMMIT;
