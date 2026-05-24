-- 0090: make audit_log append-only at the DB layer.
-- Any service-level DELETE or UPDATE becomes a silent no-op.
-- TRUNCATE is intentionally not blocked (operator maintenance; superuser path).

CREATE OR REPLACE RULE no_delete_audit_log AS
    ON DELETE TO audit_log DO INSTEAD NOTHING;

CREATE OR REPLACE RULE no_update_audit_log AS
    ON UPDATE TO audit_log DO INSTEAD NOTHING;
