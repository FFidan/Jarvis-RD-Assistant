-- 087_pulse_models_user_id_index.sql — DB-F04: missing index on pulse_models.user_id
--
-- Migration 082 added the pulse_models.user_id FK (ON DELETE SET NULL) but no
-- supporting index. Per-user pulse-model lookups and the FK's ON DELETE SET NULL
-- cascade both scan pulse_models by user_id; without an index those degrade to
-- sequential scans as the table grows.
--
-- (DB-F03 / journal_entries was verified FALSE — its (user_id, date) composite
--  index already covers user_id-prefixed lookups, so no index is added there.)
--
-- Idempotent CREATE INDEX IF NOT EXISTS. No BEGIN/COMMIT (the runner wraps each
-- migration in a transaction).

CREATE INDEX IF NOT EXISTS idx_pulse_models_user_id ON pulse_models(user_id);
