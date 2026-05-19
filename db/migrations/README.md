# db/migrations

## What changed — 2026-05-19 Wave 1 squash

The 88 incremental migration files (`001_*.sql` … `088_*.sql`) were collapsed
into a single regenerated `db/init.sql` baseline. `db/migrations/` now holds
only migrations **0089 onward** (currently empty — the next schema change adds
`0089_*.sql`).

## How fresh installs work

Postgres applies `db/init.sql` on an empty data volume via the
`/docker-entrypoint-initdb.d/01_init.sql` mount (`docker-compose.yml`). The
file embodies all of schema 1–88 and pre-marks all 88 versions applied in
`schema_migrations` with an explicit contiguous `INSERT INTO schema_migrations
(version) VALUES (1), …, (88) ON CONFLICT (version) DO NOTHING` block.

On every startup the runtime `run_migrations()` in
`libs/jarvis_common/jarvis_common/migrations.py` scans `db/migrations/` for
`NNN_*.sql` files with `N >= 89` and applies any that are not yet recorded in
`schema_migrations`. On a fresh install (all 88 pre-marked) the scan is a
no-op.

## Authoring a new migration (0089+)

1. Create `db/migrations/0089_<slug>.sql`.
2. The runner picks it up by numeric prefix (`sorted(glob("*.sql"))`).
3. Do **not** include `BEGIN`, `COMMIT`, or `ROLLBACK` at the outer level — the
   runner wraps each file in its own savepoint (`check-migrations-no-tx.sh`
   enforces this).
4. Use `$$` dollar-quoting (not `$tag$`) for PL/pgSQL bodies.
5. Guard `ADD CONSTRAINT` with `DO $$ … EXCEPTION WHEN duplicate_object THEN
   NULL END $$` for idempotency.
6. Do **not** use `generate_series` to seed `schema_migrations` — the explicit
   contiguous list is the audit trail that `init.sql` truly embodies each
   version (`check-migrations-no-tx.sh` Check 3 enforces this).

## No existing-DB migration path / no rollback

Dev DBs are rebuildable volumes. To pick up any schema change before `0089`
exists, wipe the volume:

```bash
docker compose down -v
docker compose up -d
```

There is no historical replay path and no rollback. The pre-squash 88-file
chain was never replayable from blank by construction.

## Re-baselining policy

When the `0089+` tail grows large again, repeat the regeneration mechanic
documented at
`docs/superpowers/plans/2026-05-19-wave1-baseline-mechanic.md` (§A) and
re-prove via the §D schema-equivalence test before deleting the absorbed
migrations.
