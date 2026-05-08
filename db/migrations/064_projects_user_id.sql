-- 064_projects_user_id.sql
-- Multi-tenant readiness: add user_id to projects for per-user project isolation.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_projects_user
    ON projects(user_id) WHERE user_id IS NOT NULL;
