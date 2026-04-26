-- Migration 037: Persist active per-user Pulse classifier models
BEGIN;

CREATE TABLE IF NOT EXISTS pulse_models (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER,
    model_version   TEXT NOT NULL DEFAULT 'v1',
    model_blob      BYTEA NOT NULL,
    feature_names   JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    trained_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pulse_models_one_active_per_user
    ON pulse_models (coalesce(user_id, 0))
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_pulse_models_trained_at
    ON pulse_models(trained_at DESC);

COMMIT;
