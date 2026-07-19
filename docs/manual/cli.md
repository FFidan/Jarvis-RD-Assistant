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
| `register` | Record the current checkout as the managed install and refresh the launcher. |
| `uninstall [--dry-run] [--tier N] [--keep-data] [--all] [--yes]` | Tiered, contained teardown of a managed install: stop (1), remove application images (2), delete data volumes (3), or full purge (4). Lead with `--dry-run`. See [Uninstalling](#uninstalling). |
| `version` | Print the command name and the installed `JARVIS_VERSION`. |
| `help` | Print usage. |

Run `jarvis-research help` for the built-in summary. A command can be pointed at
a specific checkout with `jarvis-research --repo <dir> <command>`.

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
4. **Verifies the release is fully published.** Every image the target needs must
   already exist in the registry; a visible tag whose images are still uploading
   is refused, so you never advance onto a half-published release.
5. **Enforces a backup before a data-changing migration.** New migrations between
   your version and the target are inspected. If any one changes data, the update
   requires a *fresh, checksum-verified* backup before it will proceed: it
   triggers an on-demand backup, waits for it, and confirms the database archive
   (and the secrets archive, when backups are encrypted) is present and intact.
   Without one it refuses. Additive-only migrations apply on restart and only
   suggest taking a restore point first.
6. **Stages images, then advances.** The full target image set is pulled *before*
   the branch moves; a failed pull aborts with the checkout untouched. Only then
   does the checkout fast-forward to the target tag.
7. **Applies and verifies health.** It pins `JARVIS_VERSION`, recreates the
   services (via `update.sh`), and waits for them to report healthy. On success
   it clears the pending-transaction record and prints a `doctor` summary.

### Resuming an interrupted update

Each update writes a small pending-transaction file that records the phase it
reached. If a run is interrupted, simply run `jarvis-research update` again — it
resumes deterministically from the recorded phase instead of reporting "up to
date". A run that stopped before advancing the branch restarts cleanly. If you
need to drive the post-advance half explicitly, `jarvis-research update --resume
<tag>` runs only those remaining steps.

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

`jarvis-research update` is the recommended path, but a checkout can always be
updated manually:

```bash
git pull
./update.sh
```

See [Deployment → Update Workflow](../DEPLOYMENT.md#update-workflow) for the
details of the manual path.

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
