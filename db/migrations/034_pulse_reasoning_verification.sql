-- WS-2.3: Pulse reasoning verification — persist per-card verified flag
-- and confidence enum so the frontend can render a trust badge.
--
-- Idempotent: additive columns only; safe to re-apply after migration 033.
ALTER TABLE pulse_cards
  ADD COLUMN IF NOT EXISTS reasoning_verified BOOLEAN DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS reasoning_confidence VARCHAR(10) DEFAULT NULL
    CHECK (reasoning_confidence IS NULL OR reasoning_confidence IN ('HIGH','MEDIUM','LOW','UNVERIFIED'));
