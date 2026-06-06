# Migrations

This directory is the ledger for post-public-launch schema migrations.

The repo's pre-launch schema is fully captured in `db/init.sql`. There are
no pre-launch migrations on disk: 0089 / 0090 / 0091 were folded into
`init.sql` on 2026-05-26 as part of the 2026-05 schema consolidation,
since the repo had never been publicly deployed at that point.

Migrations 0092–0095 are on disk; new ones land here numbered sequentially (`0096_<descriptive>.sql` and up) and are
applied via `run_migrations` (libs/jarvis_common/jarvis_common/migrations.py)
on top of `init.sql`.
