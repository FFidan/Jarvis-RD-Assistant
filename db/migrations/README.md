# Migrations

This directory is the ledger for incremental schema migrations applied on top of `db/init.sql`.

The repo's pre-launch schema is fully captured in `db/init.sql`. The schema is
created on first boot from `init.sql` alone — there are no pre-launch migrations
on disk and no manual migration steps to run.

New migrations land here numbered sequentially (`0102_<descriptive>.sql` and up)
and are applied via `run_migrations`
(libs/jarvis_common/jarvis_common/migrations.py) on top of `init.sql`.
