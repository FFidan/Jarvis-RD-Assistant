-- 081_audit_log_action_index.sql — WS-ADMIN-AUDIT
--
-- The admin audit-log reader (/api/admin/audit-log) supports an optional
-- action-prefix filter and always orders by id DESC with a created_at cursor.
-- Add a composite (action, created_at DESC) index so prefix-filtered,
-- time-ordered scans don't degrade into a full table scan as audit_log grows.
--
-- (Transaction wrapper added by the migrations runner; do not include
-- BEGIN/COMMIT here.)

CREATE INDEX IF NOT EXISTS idx_audit_log_action_created
    ON audit_log (action, created_at DESC);
