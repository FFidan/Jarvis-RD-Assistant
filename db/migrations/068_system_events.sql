-- 068: system_events table for Logs Admin UI + vector_writer role
-- system_events is for OPERATIONAL events; existing audit_log table is for SECURITY events.
-- (transaction wrapper added by migrations runner; do not include BEGIN/COMMIT)

CREATE TABLE IF NOT EXISTS system_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level TEXT NOT NULL CHECK (level IN ('debug','info','warning','error','critical')),
    category TEXT NOT NULL CHECK (category IN ('error','job','source','auth','config','infra')),
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id UUID
);
CREATE INDEX IF NOT EXISTS system_events_created_at_idx ON system_events (created_at DESC);
CREATE INDEX IF NOT EXISTS system_events_category_level_idx ON system_events (category, level, created_at DESC);
CREATE INDEX IF NOT EXISTS system_events_correlation_idx ON system_events (correlation_id, created_at) WHERE correlation_id IS NOT NULL;

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vector_writer') THEN
    -- Password set out-of-band via secret file at boot; placeholder role created here
    EXECUTE format('CREATE ROLE vector_writer LOGIN PASSWORD %L', coalesce(current_setting('jarvis.vector_writer_password', true), 'change_me_at_boot'));
  END IF;
END $$;
GRANT INSERT ON system_events TO vector_writer;
GRANT USAGE, SELECT ON SEQUENCE system_events_id_seq TO vector_writer;
