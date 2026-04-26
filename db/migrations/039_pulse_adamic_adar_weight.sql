-- Migration 039: add bounded Adamic-Adar Pulse signal weight default.
BEGIN;

UPDATE user_config
SET value = '{"citation_pagerank": 0.0, "citation_count": 0.0, "citation_adamic_adar": 0.0, "classifier": 0.0}'::jsonb
    || value,
    updated_at = NOW()
WHERE key = 'pulse.weights'
  AND jsonb_typeof(value) = 'object';

COMMIT;
