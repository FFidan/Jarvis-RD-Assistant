-- 056: canonicalize pulse.stage2_top_k to 40
-- The historical seed in migration 018 used 50, but the code-fallback in
-- paper_ingestion/pulse/profile.py (_DEFAULT_STAGE2_TOP_K) and the frontend
-- default both use 40. Drift between DB seed (50) and code default (40)
-- caused intermittent test failures and confusing UI behaviour where new
-- installs' first pulse run picked 50 but every subsequent runtime read
-- after manual config edits used 40. This migration fixes existing DBs;
-- db/init.sql is updated separately so fresh installs seed 40 directly.
UPDATE user_config SET value = '40'::jsonb WHERE key = 'pulse.stage2_top_k' AND value = '50'::jsonb;
