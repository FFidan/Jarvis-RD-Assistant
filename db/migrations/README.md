# Migration Files

Migration files are applied automatically on startup by `libs/jarvis_common/jarvis_common/migrations.py`.

## Convention

- Files are named `NNN_description.sql` (zero-padded 3-digit number)
- Do **NOT** add `BEGIN` or `COMMIT` to migration files — the runner wraps each file in its own transaction automatically
- Migration files are applied in ascending numeric order
- Already-applied migrations are tracked in the `schema_migrations` table

## Multi-Tenant Support (Migration 042–043)

**Migrations 042 and 043** add groundwork for multi-tenant operation:

- **042**: Adds `user_id` (nullable) columns to all major tables (`papers`, `paper_notes`, `paper_summaries`, `paper_chunks`, `paper_user_state`, `pulse_cards`, `paper_contradictions`, `paper_extractions`, `pulse_decks`). In single-user mode, `user_id` stays `NULL` (system-owned). Multi-tenant enforcement is gated on a real auth resolver in `jarvis_common.auth.current_user_id_or_none()`.
- **043**: Replaces single-paper UNIQUE constraints with `UNIQUE NULLS NOT DISTINCT (paper_id, user_id)` on `paper_user_state` and `paper_summaries`, enabling two users to independently track the same paper. Uses defensive PL/pgSQL introspection to handle diverged constraint names across deployments (forward compatibility for cluster migrations).
