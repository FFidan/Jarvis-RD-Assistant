# Migration Files

Migration files are applied automatically on startup by `libs/jarvis_common/jarvis_common/migrations.py`.

## Convention

- Files are named `NNN_description.sql` (zero-padded 3-digit number)
- Do **NOT** add `BEGIN` or `COMMIT` to migration files — the runner wraps each file in its own transaction automatically
- Migration files are applied in ascending numeric order
- Already-applied migrations are tracked in the `schema_migrations` table

## Multi-Tenant Support (Migrations 042–043, 077, 080, 082)

**Migrations 042 and 043** lay the groundwork for multi-tenant operation:

- **042**: Adds `user_id` (nullable) columns to all major tables (`papers`, `paper_notes`, `paper_summaries`, `paper_chunks`, `paper_user_state`, `pulse_cards`, `paper_contradictions`, `paper_extractions`, `pulse_decks`). Legacy NULL rows are matched via `NULLS NOT DISTINCT` for backward compatibility.
- **043**: Replaces single-paper UNIQUE constraints with `UNIQUE NULLS NOT DISTINCT (paper_id, user_id)` on `paper_user_state` and `paper_summaries`, enabling two users to independently track the same paper. Uses defensive PL/pgSQL introspection to handle diverged constraint names across deployments.

**Migrations 077, 080, and 082** complete FK enforcement — multi-tenant is GA as of v0.4.0. Every user-data row is isolated at the query layer; `current_user_id_strict` enforces ownership on all write paths. The first-run wizard creates the initial admin automatically; subsequent users are managed via **Settings → Admin → Users**.
