-- 075: backfill double-encoded JSONB rows (Wave 1 closeout)
--
-- Repairs rows that were double-encoded by the H1-H4 JSONB call sites
-- before commits dbfe9779/511e4bf9/7a7ea354/850be497/50cba751 fixed the
-- json.dumps(...) :: jsonb antipattern. Idempotent: rows where
-- jsonb_typeof = 'string' are unwrapped via col #>> '{}', which decodes
-- the outer JSON-string layer once and re-casts to jsonb.
--
-- Detection signature: jsonb_typeof(col) = 'string' means the stored
-- value is a JSON string literal (e.g. "{\"key\": 1}") rather than a
-- JSON object. The asyncpg JSONB codec in db_helpers.init_pg_connection
-- uses json.dumps/json.loads, so a Python dict that was already
-- json.dumps()-encoded before being passed to asyncpg gets double-encoded.
--
-- Tables fixed:
--   system_events.context     — from infra_events.bulk_ingest (H1)
--   journal_entries.prompts   — from my_day.upsert_journal_entry (H2)
--   user_config.value         — from setup._persist_config, main._autoconfigure_models_hook,
--                               and telegram._handle_pairing (H3-H4)
--
-- (Transaction wrapper added by migrations runner; do not include BEGIN/COMMIT.)

-- system_events.context: some rows may have been written multiple times by the
-- buggy call site across service restarts, accumulating more than one layer of
-- encoding. Loop until no string-typed rows remain, unwrapping one layer per
-- pass (max 10 passes as a safety cap — in practice 1-2 passes suffice).
DO $$
DECLARE
    _rows_updated INTEGER;
    _pass         INTEGER := 0;
BEGIN
    LOOP
        UPDATE system_events
        SET context = (context #>> '{}')::jsonb
        WHERE jsonb_typeof(context) = 'string';

        GET DIAGNOSTICS _rows_updated = ROW_COUNT;
        _pass := _pass + 1;
        EXIT WHEN _rows_updated = 0 OR _pass >= 10;
    END LOOP;
END $$;

DO $$
DECLARE
    _rows_updated INTEGER;
    _pass         INTEGER := 0;
BEGIN
    LOOP
        UPDATE journal_entries
        SET prompts = (prompts #>> '{}')::jsonb
        WHERE jsonb_typeof(prompts) = 'string';

        GET DIAGNOSTICS _rows_updated = ROW_COUNT;
        _pass := _pass + 1;
        EXIT WHEN _rows_updated = 0 OR _pass >= 10;
    END LOOP;
END $$;

-- user_config.value: only unwrap rows where the inner string is itself a JSON
-- object or array (starts with '{' or '['). These are the only unambiguous
-- double-encode cases — a user_config value that is legitimately a JSON string
-- (timezone, model name, cron expression, boolean-as-string) must not be
-- touched. Scalar JSON strings ("true", "10", etc.) may be intentional string
-- config values written by the app before the H3/H4 fix; leaving them as
-- JSON strings is safe since readers that expect booleans/numbers are
-- responsible for parsing.
--
-- NOTE: In the live DB (2026-05-14 snapshot) all string-typed user_config rows
-- are legitimate scalar strings — no double-encoded objects were found. This
-- UPDATE is a safety net for any environments that may have had dict values
-- written via the buggy code path.
DO $$
DECLARE
    _rows_updated INTEGER;
    _pass         INTEGER := 0;
BEGIN
    LOOP
        UPDATE user_config
        SET value = (value #>> '{}')::jsonb
        WHERE jsonb_typeof(value) = 'string'
          AND (value #>> '{}') ~ '^(\{|\[)';  -- only JSON objects and arrays

        GET DIAGNOSTICS _rows_updated = ROW_COUNT;
        _pass := _pass + 1;
        EXIT WHEN _rows_updated = 0 OR _pass >= 10;
    END LOOP;
END $$;
