-- Migration 022: Fix double-JSON-encoded user_config values (WEB-C01 Round 7)
--
-- Background: services/paper_ingestion/app/routers/settings.py and main.py previously
-- called json.dumps(value) before passing the value to asyncpg, which already has the
-- JSONB codec registered via init_pg_connection (encoder=json.dumps). This caused the
-- value to be serialised twice: a Python string "0 4 * * *" became the JSONB string
-- value '"0 4 * * *"' (a JSON-encoded string whose content is itself a JSON-encoded
-- string). The dashboard Settings editor then failed to parse the value and showed
-- "Invalid cron expression".
--
-- Detection: a double-encoded row is a JSONB string whose extracted text content
-- (value #>> '{}') looks like a JSON literal — starts with '"' (a JSON-encoded string),
-- '{' (a JSON-encoded object), '[' (array), or a digit (number).
--
-- Fix: parse the extracted inner text as JSON and store that. This is equivalent to
-- calling json.loads() once on the already-deserialized value.
--
-- All UPDATEs are idempotent: the WHERE clauses only match rows still in the broken
-- state, so re-running the migration on a clean database is a no-op.

BEGIN;

-- ── pulse.cron ───────────────────────────────────────────────────────────────
-- Double-encoded string like '"0 4 * * *"' (inner content is a JSON string literal).
UPDATE user_config
SET value = (value #>> '{}')::jsonb
WHERE key = 'pulse.cron'
  AND jsonb_typeof(value) = 'string'
  AND (value #>> '{}') LIKE '"%';

-- ── llm.smart_model ───────────────────────────────────────────────────────────
UPDATE user_config
SET value = (value #>> '{}')::jsonb
WHERE key = 'llm.smart_model'
  AND jsonb_typeof(value) = 'string'
  AND (value #>> '{}') LIKE '"%';

-- ── telegram.owner_chat_id ────────────────────────────────────────────────────
-- Could be a double-encoded integer stored as a JSON string like '"123456789"'.
UPDATE user_config
SET value = (value #>> '{}')::jsonb
WHERE key = 'telegram.owner_chat_id'
  AND jsonb_typeof(value) = 'string'
  AND (value #>> '{}') ~ '^[0-9]+$';

-- ── pulse.weights ─────────────────────────────────────────────────────────────
-- Double-encoded object: JSONB stores a string whose inner content is a JSON object.
UPDATE user_config
SET value = (value #>> '{}')::jsonb
WHERE key = 'pulse.weights'
  AND jsonb_typeof(value) = 'string'
  AND (value #>> '{}') LIKE '{%';

COMMIT;
