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

## Drift Guard: `db/init.sql` ↔ migration sequence

### The invariant

`db/init.sql` is a **hand-maintained** steady-state schema snapshot. Docker
mounts it at `/docker-entrypoint-initdb.d/01_init.sql` and runs it **once** for
a brand-new database volume. It is **not** generated — there is no generation
script. It intentionally pre-seeds `schema_migrations` only up to ~v68; later
additive/corrective migrations are deliberately left absent so the runtime
runner applies them on first boot.

**Pre-existing structural debt (do not "fix" by deleting the guard).** The
migration files are *forward-only deltas on top of the init.sql base*, not a
self-contained schema, and they are **not replayable from the current
snapshot**: e.g. `001_indexes_and_constraints.sql`'s own header says "Run this
on existing databases" and it indexes `paper_user_state(status)`, a column a
later migration removed and the current snapshot no longer has. So a naive
`schema(blank + all migrations)` vs `schema(init.sql)` comparison is
**impossible in this repo by construction** — this is tracked debt
(ROADMAP item 3), not something the guard can paper over.

The repo-honest, checkable invariant is therefore: the **delta between the
hand-curated snapshot and the real fresh-install schema is pinned**.

- **BASE** = `069_auth.sql` → `init.sql` (the snapshot; `init.sql`
  FK-references `users(id)` but never creates `users`/`sessions`, which live
  only in migration 069, so 069 is applied first).
- **FULL** = BASE → `run_migrations()` (the real Docker fresh-install path;
  `init.sql`'s pre-seed makes the runner apply only the deliberately-omitted
  tail).

`FULL \ BASE` is exactly the set of schema objects the deliberately-omitted
tail migrations add. It is non-empty at HEAD (by design — 33/34/52/53/63–66/
69+ are intentionally runtime-only) and is **pinned** to a golden manifest:
`db/migrations/.init-sql-drift-baseline.txt`. A new migration that adds/alters
a schema object `init.sql` does not embody **enlarges that delta**, so it no
longer matches the manifest and the guard fails — naming the exact new object.
This is the class of bug that broke `test_migration_046/047` (the migration
post-state disagreed with the `init.sql` snapshot).

### How the guard works

`services/paper_ingestion/tests/test_migrations_live.py::test_init_sql_matches_migration_sequence`
(opt-in: `JARVIS_RUN_LIVE_PG=1`, marker `live_pg`; wired into the CI
`cross-user-isolation` job) builds **BASE** and **FULL** on disposable
PostgreSQL, runs `pg_dump --schema-only` on each, **normalizes away
non-semantic noise** (comments, `SET`/`set_config` GUCs, ownership/privileges,
object emission order, whitespace, the `schema_migrations` data rows,
extension version strings, sequence `setval`/`OWNED BY`, the empty
`COMMENT ON SCHEMA public` pg_dump emits after the in-test schema reset), then
computes `FULL \ BASE` and asserts it equals the pinned manifest.
Table/column/type/constraint/index/trigger/function definitions are
*retained*, so a real column drift still fails. On mismatch it prints the
exact NEW (and any missing) objects plus a unified diff.

### When the guard fails — reconcile `init.sql`

A failing guard means a migration added/altered a schema object but
`db/init.sql` (the fresh-install snapshot) does not embody it. Decide which
case applies:

1. Read the **NEW objects not in baseline** list in the test output — it names
   exactly what drifted.
2. **Normal case — the snapshot should embody the change.** Hand-edit
   `db/init.sql` so a fresh install already has that shape (add the new
   column/index/constraint to the relevant `CREATE TABLE`, or add a new
   `CREATE TABLE`/seed row), matching the post-migration steady state. Do
   **not** add the migration's version to the `SCHEMA-MIGRATIONS BOOTSTRAP`
   block unless `init.sql` now *fully* embodies it; never use `generate_series`
   to blanket-seed versions (`scripts/check-migrations-no-tx.sh` enforces
   this). After this the delta shrinks back to the pinned manifest and the
   guard passes — **do not touch the baseline file.**
3. **Deliberate runtime-only tail addition** (the migration is intentionally
   left out of the snapshot, like the existing 33/34/52/53/63–66 set). Then,
   and only then, **regenerate the baseline** as a reviewed, explicit action
   and commit the manifest change alongside the migration so reviewers see the
   new pinned object:

   ```bash
   # Regenerate db/migrations/.init-sql-drift-baseline.txt
   JARVIS_RUN_LIVE_PG=1 JARVIS_REGEN_DRIFT_BASELINE=1 uv run pytest \
     services/paper_ingestion/tests/test_migrations_live.py::test_init_sql_matches_migration_sequence -q
   ```

4. Re-run the guard until it is green again:

   ```bash
   JARVIS_RUN_LIVE_PG=1 uv run pytest \
     services/paper_ingestion/tests/test_migrations_live.py::test_init_sql_matches_migration_sequence -q
   ```
