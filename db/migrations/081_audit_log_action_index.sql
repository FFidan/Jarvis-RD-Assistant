-- 081_audit_log_action_index.sql — WS-ADMIN-AUDIT
--
-- The admin audit-log reader (/api/admin/audit-log) supports an optional
-- action-prefix filter and always orders by id DESC. Add a composite
-- (action, timestamp DESC) index so prefix-filtered, time-ordered scans
-- don't degrade into a full table scan as audit_log grows.
--
-- Note: the column is named "timestamp" (not created_at); the router aliases
-- it as created_at in the SELECT so the API response field stays stable.
-- The index name is distinct from mig 030's idx_audit_log_action so
-- IF NOT EXISTS is harmless on fresh installs.
--
-- (Transaction wrapper added by the migrations runner; do not include
-- BEGIN/COMMIT here.)

CREATE INDEX IF NOT EXISTS idx_audit_log_action_created
    ON audit_log (action, "timestamp" DESC);
