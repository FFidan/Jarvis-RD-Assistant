-- 065_tasks_user_id.sql
-- Multi-tenant readiness: add user_id to tasks for per-user task isolation.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_user
    ON tasks(user_id) WHERE user_id IS NOT NULL;
