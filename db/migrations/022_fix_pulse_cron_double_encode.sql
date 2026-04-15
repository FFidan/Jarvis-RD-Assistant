-- Migration 022: Fix double-JSON-encoded user_config values (WEB-C01 Round 7)
--
-- Background: services/paper_ingestion/app/routers/settings.py previously called
-- json.dumps(body.value) before passing the value to asyncpg, which already has the
-- JSONB codec registered via init_pg_connection (encoder=json.dumps). This caused the
-- value to be serialised twice.
--
-- Detection: a double-encoded JSONB string value starts with a literal quote character
-- inside the JSON text form. For example the cron string "0 4 * * *" would be stored
-- as the JSON text '"\"0 4 * * *\""' — i.e. the JSONB::text output starts with '"\"'.
-- We match rows where value::text ~ '^"\\"' (regex: starts with a double-quote
-- immediately followed by a backslash-quote) to identify only affected rows.
--
-- For pulse.weights (a JSON object), double-encoding produces a JSONB string whose
-- text content, when parsed as JSON, yields the actual object. Detection: value is a
-- JSONB string type (value::text starts with '"') AND its inner content parses as an
-- object (the inner text starts with '{').
--
-- Fix: strip the outer JSON string layer with trim(both '"' from ...) and re-wrap via
-- to_jsonb for string keys, or cast the inner text back to jsonb for object keys.
--
-- All UPDATEs are wrapped in a single transaction and are idempotent (the WHERE
-- conditions only match rows that are still in the broken state).

BEGIN;

-- ── pulse.cron ───────────────────────────────────────────────────────────────
-- Double-encoded string: JSONB text looks like '"\"0 4 * * *\""'
-- Fix: strip surrounding quotes, unescape inner backslash-quotes, re-wrap as jsonb string.
UPDATE user_config
SET value = to_jsonb(
        replace(
            trim(both '"' from value::text),
            '\"', '"'
        )
    )
WHERE key = 'pulse.cron'
  AND value::text ~ '^"\\"';

-- ── telegram.owner_chat_id ────────────────────────────────────────────────────
-- Could be a double-encoded integer stored as a JSON string, e.g. '"123456789"'
-- (starts with '"' but should be a bare number).
-- Detection: value is a jsonb string (starts with '"') but should be a number.
UPDATE user_config
SET value = to_jsonb(
        (trim(both '"' from value::text))::bigint
    )
WHERE key = 'telegram.owner_chat_id'
  AND value::text ~ '^"[0-9]'
  AND jsonb_typeof(value) = 'string';

-- ── pulse.weights ─────────────────────────────────────────────────────────────
-- Double-encoded object: JSONB stores a string whose content is a JSON object, e.g.
-- '"{\"recency\":0.4,...}"'. Detection: value is a jsonb string AND the inner text
-- starts with '{' (i.e. it is a serialised JSON object).
-- Fix: parse the inner string as jsonb.
UPDATE user_config
SET value = (trim(both '"' from
                 replace(
                     replace(value::text, '\"', '"'),
                     '\\\\', '\\'
                 )
             ))::jsonb
WHERE key = 'pulse.weights'
  AND jsonb_typeof(value) = 'string'
  AND trim(both '"' from value::text) ~ '^\{';

-- ── llm.smart_model ───────────────────────────────────────────────────────────
-- Double-encoded string: e.g. '"\"mistral-nemo\""'
UPDATE user_config
SET value = to_jsonb(
        replace(
            trim(both '"' from value::text),
            '\"', '"'
        )
    )
WHERE key = 'llm.smart_model'
  AND value::text ~ '^"\\"';

COMMIT;
