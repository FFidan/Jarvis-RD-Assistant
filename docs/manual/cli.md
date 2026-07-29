# The `jarvis-research` command

`jarvis-research` is the lifecycle command for a JARVIS install. The installer
(`./setup.sh` or `scripts/jarvis-setup.sh`) puts a small launcher on your PATH
at `~/.local/bin/jarvis-research` and records your checkout as the managed
install, so you can run day-to-day operations from any directory.

The launcher carries no logic of its own: it finds your most recently installed
checkout and hands off to that repository's tracked script. An update therefore
ships a newer command along with the rest of the code.

If `jarvis-research` is not found after an install, make sure `~/.local/bin` is
on your PATH, or run `./setup.sh` again from your checkout.

## Commands

| Command | What it does |
| --- | --- |
| `update [--to <tag>] [--resume <tag>] [--yes]` | Transactional, database-safe upgrade to the latest published release, or to a specific `--to` tag. Refuses on a diverged, dirty, or non-`main` checkout and when the target's images are not yet published. |
| `status` | Show container status (`docker compose ps`). |
| `start` | Start the stack without building (`docker compose up -d --no-build`). |
| `stop` | Stop the stack. |
| `restart` | Restart the stack. |
| `logs [args]` | Tail service logs; extra arguments pass through to `docker compose logs` (e.g. `logs -f paper_ingestion`). |
| `doctor` | Read-only health, disk, registration, and update-availability check, plus host preflight probes. |
| `repair` | Bounded, non-destructive recovery: recreate stopped containers (no build, no pull) and restart any unhealthy mandatory service. |
| `owner status` | Show whether instance ownership comes from the host environment or database and whether it resolves to a live administrator. |
| `owner set <email>` | Repair a missing or invalid database-managed owner. Refuses host-managed ownership and requires the target email to be typed again. |
| `restore acknowledge <restore-id>` | After an off-host restore, release outbound-integration quarantine for the exact reviewed restore. Requires the restore ID to be typed again. |
| `register` | Record the current checkout as the managed install and refresh the launcher. |
| `uninstall [--dry-run] [--tier N] [--keep-data] [--all] [--yes]` | Tiered, contained teardown of a managed install: stop (1), remove application images (2), delete data volumes (3), or full purge (4). Lead with `--dry-run`. See [Uninstalling](#uninstalling). |
| `version` | Print the command name and the installed `JARVIS_VERSION`. |
| `help` | Print usage. |

Run `jarvis-research help` for the built-in summary. A command can be pointed at
a specific checkout with `jarvis-research --repo <dir> <command>`.

### Ownership recovery

Run `jarvis-research owner status` after upgrading an installation that already
had multiple administrators. Migration 0105 assigns the sole live
administrator automatically, but deliberately leaves a multi-admin choice to
the operator. If the database owner is missing or invalid, use
`jarvis-research owner set <admin-email>` and type the same email to confirm.
The command accepts only a live administrator and commits the repair atomically.
When `OWNER_USER_ID` manages ownership, change that host setting and restart
JARVIS instead. A valid database-managed owner transfers ownership in **Admin →
User Management**, not through the repair command.

### Restore acknowledgement

An off-host restore quarantines restored outbound integrations until their
credentials have been reviewed. Use the browser progress view or run
`jarvis-research restore acknowledge <restore-id>` with the exact current
32-character restore ID, then type it again. A missing, stale, or changed review
state is refused and leaves quarantine active. See [Backup &
Restore](backup-and-restore.md#identity-and-credential-boundaries).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The operation was refused or failed. |
| `2` | Usage error (unknown command or option). |
| `3` | Environment problem — the Docker daemon is not reachable. |

## How `update` works

`jarvis-research update` is transactional: it verifies the whole target release
before it changes anything, advances your checkout only by a fast-forward, and
records its progress so an interrupted run can resume. It never force-rewrites
your branch.

In order, an update:

1. **Checks preconditions.** The checkout must be registered, its `origin` must
   be the managed JARVIS repository, the Docker daemon must be reachable, and the
   checkout must be a clean, non-detached `main` (uncommitted changes, a detached
   HEAD, or a working branch are all refused with guidance).
2. **Selects a target.** With no argument it picks the highest published stable
   release; `--to <tag>` selects a specific tag.
3. **Requires a fast-forward.** If your checkout has diverged from the target so
   that a fast-forward is impossible, the update is refused rather than forced.
   If you are already on the target, it reports "already up to date".
4. **Verifies the release is fully published.** Every registry-backed image the
   target needs must already exist; a visible tag whose images are still
   uploading is refused, so you never advance onto a half-published release.
5. **Enforces a restore point before a data-changing migration.** New migrations
   between your version and the target are inspected. If any one changes data,
   the command triggers a backup and accepts it only when its signed manifest
   names the exact main-database, model-router, uploaded-PDF, and secrets
   archives for that run and their sizes and checksums match. A missing,
   unsigned, incomplete, or mismatched point is refused. Additive-only
   migrations apply on restart and only suggest taking a restore point first.
6. **Stages images, then advances.** The target's Compose files are resolved with
   this install's active profiles. Every exact registry image in that result,
   including profile dependencies and target-added services, is pulled *before*
   the branch moves. Services explicitly marked for local builds are not sent to
   a registry. A failed pull leaves the checkout untouched.
7. **Applies and verifies health.** It pins `JARVIS_VERSION`, recreates the
   services (via `update.sh`), and waits for them to report healthy. On success
   it clears the pending-transaction record and prints a `doctor` summary.

### Resuming an interrupted update

Each install writes its own small pending-transaction file, so interrupted
updates in two clones cannot overwrite each other. On the first update after
upgrading from an older command, JARVIS moves the former shared record only when
its recorded commits identify this install unambiguously; otherwise it stops and
keeps the record untouched. If a run is interrupted, run
`jarvis-research update` again — it
resumes deterministically from the recorded phase instead of reporting "up to
date", even when the checkout already points at the target commit. A run that
stopped before advancing the branch restarts cleanly. If you need to drive the
post-advance half explicitly, `jarvis-research update --resume <tag>` runs only
those remaining steps. The command refuses to resume if the checkout moved to
an unexpected commit.

### Updating from v1.1.3

The lifecycle command shipped with v1.1.3 predates the backup protocol required
by v1.2.2. From the v1.1.3 installation directory, run the v1.2.2 bootstrap
once:

```bash
(
  set -e
  bootstrap="$(mktemp)"
  trap 'rm -f "$bootstrap"' EXIT
  curl -fsSL -o "$bootstrap" \
    https://raw.githubusercontent.com/limitcycle-oss/jarvis-rd-assistant/v1.2.2/scripts/update-bootstrap.sh
  bash "$bootstrap" --repo "$PWD" --to v1.2.2
)
```

The bootstrap accepts only the managed repository on a clean `main` checkout,
validates v1.2.2 against `origin`, and runs the lifecycle files stored in that
release. The v1.2.2 updater then creates and authenticates a restore point
containing both databases, uploaded PDFs and data-coupled secrets before it can
apply a data-changing migration. If the command is interrupted, run the same
bootstrap command again; it resumes the recorded update.

Installations running v1.2.0 or v1.2.1 do not need the bootstrap and update
normally with `jarvis-research update`.

### Rolling back

`update` never rolls back on its own. When an update fails after the images
changed, it prints the exact commands to pin `JARVIS_VERSION` back to your
previous version and pull those images.

If a data-changing migration already ran, **image rollback alone is not
schema-safe** — the new database schema stays in place. To return to the
pre-update state, restore the backup taken before the update (the WebUI Backup
panel → Restore, or `scripts/restore.sh`); that rolls the database back together
with the images. See [Backup & Restore](backup-and-restore.md).

### Release-candidate tags are throwaway

An operator who checks out a release candidate with `--to <rc-tag>` is on a
scratch checkout. Because a release squash-merges its work, the stable tag lands
on a *different* commit than the rc, so an rc checkout cannot fast-forward to the
stable tag — the diverged-checkout refusal in step 3 fires by design. A normal
`jarvis-research update` never selects rc tags; it only considers stable
`vX.Y.Z` releases. To run a stable release after trying a candidate, do a fresh
install rather than trying to update the rc checkout in place.

## Updating by hand

`jarvis-research update` is the supported path for v1.2.0 and later; v1.1.3
uses the bootstrap above. Use the lower-level fallback only to repair a
lifecycle command that cannot run, and only after independently verifying a
current restore point:

```bash
git pull --ff-only
./update.sh --yes
```

The fallback does not classify migrations, require a signed restore point, or
resume a recorded transaction. It is not a migration-safe substitute for either
supported update path.

## Uninstalling

`jarvis-research uninstall` removes an install in four escalating tiers. Each tier
enumerates the exact containers, images, volumes, and files it will remove and
confirms before acting — nothing is removed by a bulk `docker … prune`. Preview any
teardown first, which mutates nothing:

```bash
jarvis-research uninstall --dry-run --all
```

| Tier | What it removes | Reversible? |
| --- | --- | --- |
| `1` stop | Containers and the `jarvis` network (`docker compose down`). Data, images, and files are kept. | Yes — `jarvis-research start`. |
| `2` app | Tier 1 + the JARVIS application images (`ghcr.io/limitcycle-oss/jarvis-*`) at your pinned version. | Yes — a reinstall pulls them again. |
| `3` data | Tier 2 + the project's named data volumes (`docker compose down --volumes`): the database, the vector store, and the caches. | **No.** Back up first. |
| `4` purge | Tier 3 + the pinned third-party images (each confirmed individually), `.env`, `secrets/`, `shared/`, this install's registry line, the `jarvis-research` launcher (only if no other install remains), and finally the clone directory itself. | **No.** |

### Flags

| Flag | Effect |
| --- | --- |
| `--dry-run` | Enumerate everything the chosen tier would remove and exit without changing anything. |
| `--tier N` | Run tier `N` (1–4) directly instead of the interactive menu. |
| `--keep-data` | Cap the run at tier 2, so the data volumes and on-disk files are always preserved. |
| `--all` | Select tier 4 (full purge). |
| `--yes` | Skip the ordinary per-tier `[y/N]` prompt. Requires an explicit `--tier N` (or `--all`). |

### The destructive gates require typed confirmation

`--yes` and `--all` skip only the ordinary confirmation. They can never satisfy the
destructive gates, each of which reads a typed confirmation from stdin:

- **Tier 3** requires typing the compose project name before any volume is deleted.
  When the stack is running, an interactive run first offers to take a backup
  inside the backup service, where the required credentials and storage are
  available. If you accept and that backup fails or another backup is still in
  progress, uninstall stops before the typed deletion gate.
- **Tier 4** first offers to copy the backup encryption key
  (`secrets/backup_encrypt_key.txt`) to a path **outside** the clone. That key is
  deliberately excluded from backup archives, so deleting `secrets/` without it
  leaves every encrypted off-host backup permanently unrecoverable. If you decline
  the export you must type an explicit acknowledgement phrase to continue.
- **Tier 4** confirms each third-party image (postgres, ollama, qdrant, caddy, …)
  individually, since those images may be shared with other projects on the host.

A run with no controlling terminal (closed stdin) therefore cannot complete tier 3
or tier 4 — the typed gates refuse and the install is left untouched.

### When Docker is not running

Every tier needs the Docker daemon. If it is unreachable, `uninstall` refuses with
exit code 3 and prints an inventory of the orphaned containers, volumes, network,
and on-disk files so you can start Docker (then re-run) or clean up by hand. There
is no file-only teardown path.
