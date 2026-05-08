-- 062_daily_log_user_id.sql
-- Multi-tenant readiness: add user_id column to daily_log + UNIQUE NULLS NOT DISTINCT.
-- Migrations 063-066 add user_id to paper_recommendations, projects, tasks, milestones.
ALTER TABLE daily_log ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE daily_log DROP CONSTRAINT IF EXISTS daily_log_log_date_key;
DO $$ BEGIN
    ALTER TABLE daily_log ADD CONSTRAINT daily_log_user_id_log_date_key
        UNIQUE NULLS NOT DISTINCT (user_id, log_date);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
