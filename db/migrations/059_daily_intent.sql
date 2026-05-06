-- 059_daily_intent.sql — Today's Intent persistence (Phase 1g)
CREATE TABLE IF NOT EXISTS daily_intent (
  user_id TEXT NOT NULL,
  intent_date DATE NOT NULL,
  intent_text TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, intent_date)
);
