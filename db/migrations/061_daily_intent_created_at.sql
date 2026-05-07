-- 061_daily_intent_created_at.sql
-- Backfill created_at for legacy daily_intent rows; init.sql installs already have it.
ALTER TABLE daily_intent ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
