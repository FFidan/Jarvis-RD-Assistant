-- 0105: Bridge upgraded deployments to the explicit instance-owner model.
-- Only one unambiguous live administrator may be selected automatically.
WITH live_admin AS MATERIALIZED (
    SELECT id
    FROM users
    WHERE role = 'admin' AND deleted_at IS NULL
), inserted_owner AS (
    INSERT INTO user_config (user_id, key, value)
    SELECT NULL, 'owner.user_id', to_jsonb(live_admin.id)
    FROM live_admin
    WHERE (SELECT COUNT(*) FROM live_admin) = 1
      AND NOT EXISTS (
          SELECT 1
          FROM user_config
          WHERE user_id IS NULL AND key = 'owner.user_id'
      )
    RETURNING value
)
INSERT INTO audit_log (user_id, action, resource, metadata)
SELECT
    value #>> '{}',
    'owner.backfilled',
    'owner.user_id',
    jsonb_build_object('source', 'migration_0105', 'owner_user_id', value)
FROM inserted_owner;
