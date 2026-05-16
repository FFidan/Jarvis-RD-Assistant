-- 086_review_logs_idempotency.sql — per-user dedupe key for offline review sync.
ALTER TABLE review_logs ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_logs_user_idempotency
    ON review_logs (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
