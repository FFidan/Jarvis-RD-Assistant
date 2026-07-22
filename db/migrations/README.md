# Migrations

This directory is the ledger for incremental schema migrations applied on top of `db/init.sql`.

The repository's squashed baseline schema version is `101`; it is fully captured
in `db/init.sql`. A fresh database starts at that baseline and then applies the
incremental files below. The current schema version is `106`, also recorded in
`db/SCHEMA_VERSION`.

New migrations land here numbered sequentially (`0102_<descriptive>.sql` and up)
and are applied via `run_migrations`
(libs/jarvis_common/jarvis_common/migrations.py) on top of `init.sql`.

| Version | Migration | Purpose |
|---|---|---|
| `0102` | `0102_webauthn_credentials.sql` | Add passkey credentials and single-use WebAuthn challenges. |
| `0103` | `0103_purge_group_chat_pairings.sql` | Remove Telegram pairings that were created outside private chats. |
| `0104` | `0104_drop_paper_chunks_user_ownership.sql` | Remove obsolete chunk-level ownership; paper visibility is enforced at the paper boundary. |
| `0105` | `0105_backfill_owner_user_id.sql` | Assign an unambiguous sole administrator as instance owner and leave ambiguous upgrades for explicit host repair. |
| `0106` | `0106_paper_visibility_scope.sql` | Persist source-aware public/private paper scope, defaulting unknown and client-driven material to private. |

The migration runner serializes application with PostgreSQL advisory lock 42,
records each applied version in `schema_migrations`, and refuses files newer
than the running code during restore. Operators should not execute individual
SQL files by hand.
