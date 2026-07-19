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
| `uninstall` | Remove a managed install. Not yet available in this release; the command reports so and exits non-zero. |
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
