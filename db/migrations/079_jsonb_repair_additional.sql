-- 079: backfill double-encoded JSONB for audit_log + job_progress (Wave 2 closeout)
--
-- Migration 075 repaired triple-encoded JSONB for:
--   system_events.context, journal_entries.prompts, user_config.value
--
-- This migration extends the same converging-loop UPDATE pattern to the
-- columns missed in 075:
--   audit_log.metadata    — JSONB NOT NULL DEFAULT '{}', added by migration 030
--   job_progress.result   — JSONB nullable, added by migration 058
--   job_progress.error    — JSONB nullable, added by migration 058
--
-- Detection signature: jsonb_typeof(col) = 'string' means the stored value
-- is a JSON string literal (e.g. "{\"key\": 1}") rather than a JSON object.
-- Each DO block loops until no string-typed rows remain (max 10 passes).
--
-- (Transaction wrapper added by migrations runner; do not include BEGIN/COMMIT.)

-- audit_log.metadata
DO $$
DECLARE
    _rows_updated INTEGER;
    _pass         INTEGER := 0;
BEGIN
    LOOP
        UPDATE audit_log
        SET metadata = (metadata #>> '{}')::jsonb
        WHERE jsonb_typeof(metadata) = 'string';

        GET DIAGNOSTICS _rows_updated = ROW_COUNT;
        _pass := _pass + 1;
        EXIT WHEN _rows_updated = 0 OR _pass >= 10;
    END LOOP;
END $$;

-- job_progress.result (nullable — skip NULLs implicitly via WHERE)
DO $$
DECLARE
    _rows_updated INTEGER;
    _pass         INTEGER := 0;
BEGIN
    LOOP
        UPDATE job_progress
        SET result = (result #>> '{}')::jsonb
        WHERE result IS NOT NULL
          AND jsonb_typeof(result) = 'string';

        GET DIAGNOSTICS _rows_updated = ROW_COUNT;
        _pass := _pass + 1;
        EXIT WHEN _rows_updated = 0 OR _pass >= 10;
    END LOOP;
END $$;

-- job_progress.error (nullable — skip NULLs implicitly via WHERE)
DO $$
DECLARE
    _rows_updated INTEGER;
    _pass         INTEGER := 0;
BEGIN
    LOOP
        UPDATE job_progress
        SET error = (error #>> '{}')::jsonb
        WHERE error IS NOT NULL
          AND jsonb_typeof(error) = 'string';

        GET DIAGNOSTICS _rows_updated = ROW_COUNT;
        _pass := _pass + 1;
        EXIT WHEN _rows_updated = 0 OR _pass >= 10;
    END LOOP;
END $$;
