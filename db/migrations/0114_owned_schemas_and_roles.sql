-- Install the v1.2.6 physical ownership boundary.  Existing objects move by
-- catalog identity, preserving their data, constraints, indexes, triggers, and rules.

-- The bootstrap service grants this temporary membership only on a pre-0114
-- upgrade.  New databases are already at the final shape and skip this file.
SET LOCAL ROLE jarvis_legacy_rollback;

DO $$
DECLARE
    domain record;
    object_name text;
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
    END LOOP;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
END $$;

-- The migrator assumes each target owner only long enough to create its schema
-- and grant the legacy owner the CREATE required for the ownership move.
RESET ROLE;
DO $$
DECLARE
    domain record;
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
        EXECUTE format('SET LOCAL ROLE %I', domain.owner_role);
        IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = domain.schema_name) THEN
            EXECUTE format('CREATE SCHEMA %I AUTHORIZATION %I', domain.schema_name, domain.owner_role);
        END IF;
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', domain.schema_name);
        EXECUTE format(
            'GRANT USAGE, CREATE ON SCHEMA %I TO jarvis_legacy_rollback',
            domain.schema_name
        );
        EXECUTE 'RESET ROLE';
    END LOOP;
END $$;

SET LOCAL ROLE jarvis_legacy_rollback;
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

END $$;

-- Drop the temporary schema capability under each target owner before the
-- migration exposes the final grant surface.
RESET ROLE;
DO $$
DECLARE
    domain record;
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
        EXECUTE format('SET LOCAL ROLE %I', domain.owner_role);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM jarvis_legacy_rollback', domain.schema_name);
        EXECUTE 'RESET ROLE';
    END LOOP;
END $$;

SET LOCAL ROLE jarvis_ops_owner;
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

RESET ROLE;
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
        EXECUTE format('SET LOCAL ROLE %I', domain.owner_role);
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
        EXECUTE 'RESET ROLE';
    END LOOP;

    -- Transitional seams are intentionally relation-specific until their API replacements land.
    EXECUTE 'SET LOCAL ROLE jarvis_platform_owner';
    FOREACH object_name IN ARRAY ARRAY['audit_log','magic_link_tokens','sessions','system_events','user_config','users','webauthn_challenges'] LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON platform.%I TO jarvis_research_runtime', object_name);
    END LOOP;
    FOREACH object_name IN ARRAY ARRAY['llm_usage_log','user_config','users'] LOOP
        EXECUTE format('GRANT SELECT ON platform.%I TO jarvis_learning_runtime', object_name);
    END LOOP;
    GRANT INSERT, UPDATE ON platform.user_config TO jarvis_learning_runtime;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA platform TO jarvis_research_runtime, jarvis_learning_runtime;
    GRANT USAGE ON SCHEMA platform TO jarvis_research_runtime, jarvis_learning_runtime;
    EXECUTE 'RESET ROLE';

    EXECUTE 'SET LOCAL ROLE jarvis_learning_owner';
    FOREACH object_name IN ARRAY ARRAY['cards','daily_log','decks','journal_entries','milestones','project_papers','projects','review_logs','scheduled_nudges','task_paper_links','tasks'] LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON learning.%I TO jarvis_research_runtime', object_name);
    END LOOP;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA learning TO jarvis_research_runtime;
    GRANT USAGE ON SCHEMA learning TO jarvis_research_runtime;
    EXECUTE 'RESET ROLE';

    EXECUTE 'SET LOCAL ROLE jarvis_ops_owner';
    FOREACH object_name IN ARRAY ARRAY['job_progress','procrastinate_events','procrastinate_jobs','procrastinate_periodic_defers','procrastinate_workers'] LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ops.%I TO jarvis_research_runtime, jarvis_learning_runtime', object_name);
    END LOOP;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO jarvis_research_runtime, jarvis_learning_runtime;
    GRANT USAGE ON SCHEMA ops TO jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
    GRANT SELECT ON ops.schema_migrations TO jarvis_platform_runtime, jarvis_research_runtime, jarvis_learning_runtime;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ops TO jarvis_research_runtime, jarvis_learning_runtime;
    EXECUTE 'RESET ROLE';

    EXECUTE 'SET LOCAL ROLE jarvis_research_owner';
    FOREACH object_name IN ARRAY ARRAY['paper_chunks','paper_recommendations','paper_summaries','paper_user_state','paper_user_zotero_links','papers','thread','user_library'] LOOP
        EXECUTE format('GRANT SELECT ON research.%I TO jarvis_learning_runtime', object_name);
    END LOOP;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO jarvis_learning_runtime;
    GRANT USAGE ON SCHEMA research TO jarvis_learning_runtime;
    EXECUTE 'RESET ROLE';

END $$;

SET LOCAL ROLE jarvis_ops_owner;
