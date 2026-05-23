-- 0089: drop the dead pdf_resolutions table.
--
-- Verified at HEAD: zero non-test callers across services/, libs/, scripts/.
-- Only references were:
--   - db/init.sql:960 (schema definition — will be removed by this migration's apply)
--   - services/paper_ingestion/tests/pulse_helpers.py (test stub — also removed in this commit)
-- Audit reference: docs/audit/2026-05-23-deep-audit/wave3/C3-yagni-kiss-deadcode.md (Cycle 3 W2.T4).

DROP TABLE IF EXISTS pdf_resolutions CASCADE;
