# Migrations

This directory is the ledger for incremental schema migrations applied on top of `db/init.sql`.

The repository's squashed baseline schema version is `101`; it is fully captured
in `db/init.sql`. A fresh database starts at that baseline and then applies the
incremental files below. The current schema version is `120`, also recorded in
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
| `0107` | `0107_scope_contradiction_uniqueness_to_owner.sql` | Add the owner to the contradiction uniqueness key so each account keeps its own row for a shared pair of quotes. |
| `0108` | `0108_record_zotero_analysis_enqueue.sql` | Record when a Zotero import's analysis scheduling was resolved, and how many attempts it has spent, so a poll can retry an enqueue that failed without re-scheduling items it already handled or retrying one item forever. |
| `0109` | `0109_track_paper_content_generation.sql` | Stamp PDF-derived results and retained user work with the paper content generation that produced or contextualized them. |
| `0110` | `0110_require_contradiction_owner.sql` | Preserve historical contradiction evidence while requiring ownership for new writes and separating evidence produced from different paper generations. |
| `0111` | `0111_full_digest_local_ids.sql` | Identify locally uploaded papers by their full content digest, deriving the new identifier from the stored source URL where the short form is unambiguous. |
| `0112` | `0112_durable_focus_sessions.sql` | Store one authoritative active or paused focus interval per user so Web and Telegram share transitions and once-only accounting. |
| `0113` | `0113_restore_zotero_project_collections.sql` | Restore the per-project Zotero collection cache used by project-linked citation exports. |
| `0114` | `0114_owned_schemas_and_roles.sql` | Move declared objects into owned schemas and install the role, grant, search-path, and migration-integrity boundary. |
| `0115` | `0115_cross_domain_boundaries.sql` | Add owner-local domain delivery, audit identity indirection, and durable erasure coordination. |
| `0116` | `0116_unified_job_facade.sql` | Move the public jobs facade to Platform and enforce durable queue ownership. |
| `0117` | `0117_owner_capabilities.sql` | Replace remaining foreign runtime writes with owner-local delivery and exact database capabilities. |
| `0118` | `0118_enforce_runtime_privileges.sql` | Revoke transitional cross-domain grants and enforce capability-only runtime mutations. |
| `0119` | `0119_erasure_executor_capability.sql` | Restrict due-erasure selection to the executor capability and align visibility checkpoint guards with the canonical key. |
| `0120` | `0120_erasure_capability_boundary.sql` | Move erasure state changes and the account deletion clock behind owner-defined capabilities. |

The migration runner serializes application with PostgreSQL advisory lock 42,
records each applied version in `schema_migrations`, and refuses files newer
than the running code during restore. Operators should not execute individual
SQL files by hand.

Owner/ACL-free backups deliberately omit role-bound database authority. The
on-demand recovery workflow reapplies the manifest-pinned `db/restore-authority.sql`
only after forward migration reaches the packaged schema; it is not a migration
and must not be invoked independently.

## Adding a migration

The schema version is recorded in several places on purpose: a running
deployment, a fresh install, and a restore each reach it by a different route.
All of them must move together, and each is guarded by a test that fails when
one is forgotten.

1. `db/migrations/NNNN_<descriptive>.sql` — the forward step itself.
2. `db/SCHEMA_VERSION` — the packaged version.
3. This file — the current-version sentence above and a row in the table.
4. `db/init.sql` — embody the step for fresh installs, add the version to the
   `schema_migrations` seed list, and record the file's SHA-256.
5. `db/ownership-manifest.json` — the migration's hash, and the restore
   authority's hash and schema version if that file changed.
6. `db/restore-authority.sql` — its packaged-schema pin, and any grant the step
   introduced, so a restored deployment matches a fresh one.
7. `libs/jarvis_common/jarvis_common/migrations.py` — the in-code fallback used
   where the version file is unavailable.
8. `scripts/restore.sh` — the last-resort ceiling used when neither the
   migrations directory nor the version file can be read.
