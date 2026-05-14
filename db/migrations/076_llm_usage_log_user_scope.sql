-- 076_llm_usage_log_user_scope.sql
-- Per-user cost scoping: adds user_id column to llm_usage_log so that the
-- /api/analytics/llm-cost handler can be filtered to the calling user.
-- Nullable to preserve existing rows as "system-shared" (NULL user_id is
-- treated as visible only to callers who are also NULL, matching the project
-- convention from migrations 062–073).
-- (Transaction wrapper added by the migrations runner; do not include
-- BEGIN/COMMIT here.)
ALTER TABLE llm_usage_log ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_llm_usage_log_user_id ON llm_usage_log(user_id) WHERE user_id IS NOT NULL;
