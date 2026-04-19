# Migration Files

Migration files are applied automatically on startup by `libs/jarvis_common/jarvis_common/migrations.py`.

## Convention

- Files are named `NNN_description.sql` (zero-padded 3-digit number)
- Do **NOT** add `BEGIN` or `COMMIT` to migration files — the runner wraps each file in its own transaction automatically
- Migration files are applied in ascending numeric order
- Already-applied migrations are tracked in the `schema_migrations` table
