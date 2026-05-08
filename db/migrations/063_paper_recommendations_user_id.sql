-- 063_paper_recommendations_user_id.sql
-- Multi-tenant readiness: add user_id to paper_recommendations + per-user unique constraint.
ALTER TABLE paper_recommendations ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_paper_recommendations_user
    ON paper_recommendations(user_id) WHERE user_id IS NOT NULL;
DO $$ BEGIN
    ALTER TABLE paper_recommendations DROP CONSTRAINT IF EXISTS uq_paper_recommendations_paper_id;
    ALTER TABLE paper_recommendations ADD CONSTRAINT uq_paper_recommendations_paper_user_id
        UNIQUE NULLS NOT DISTINCT (paper_id, user_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
