-- 067: Pulse source health + run history + deck stale columns
-- (transaction wrapper added by migrations runner; do not include BEGIN/COMMIT)

CREATE TABLE IF NOT EXISTS source_health (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    last_request_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_status TEXT,            -- 'ok' | 'rate_limit' | 'error'
    cooldown_until TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT source_health_user_source UNIQUE NULLS NOT DISTINCT (user_id, source_type)
);
CREATE INDEX IF NOT EXISTS ix_source_health_lookup ON source_health (user_id, source_type);

CREATE TABLE IF NOT EXISTS source_run_history (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,         -- 'ok' | 'rate_limit' | 'error' | 'cooldown_skip'
    candidate_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    detail JSONB
);
CREATE INDEX IF NOT EXISTS ix_source_run_history_timeline ON source_run_history (user_id, source_type, started_at DESC);

ALTER TABLE pulse_decks ADD COLUMN IF NOT EXISTS is_stale BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pulse_decks ADD COLUMN IF NOT EXISTS stale_from_deck_id INTEGER REFERENCES pulse_decks(id) ON DELETE SET NULL;
