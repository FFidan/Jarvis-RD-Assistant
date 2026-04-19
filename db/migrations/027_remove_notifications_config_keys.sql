-- Migration 027: Remove stale notifications.* keys from user_config.
-- These keys were superseded by the scheduled_nudges table.
-- The backend whitelist already rejects them; this cleans up any rows
-- that may have been seeded by older versions.
DELETE FROM user_config WHERE key LIKE 'notifications.%';
