-- 066_milestones_user_id.sql
-- Multi-tenant readiness: add user_id to milestones for per-user milestone isolation.
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_milestones_user
    ON milestones(user_id) WHERE user_id IS NOT NULL;
