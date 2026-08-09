# Release Process

JARVIS RD Assistant publishes stable releases from reviewed commits on `main`.
Each release has an annotated Semantic Versioning tag and a GitHub Release.
Prerelease tags are not part of the supported release process.

Releases follow [Semantic Versioning](https://semver.org/):

- `MAJOR` — breaking API or schema changes that require manual operator steps.
- `MINOR` — new features that existing deployments can adopt through the
  managed update command.
- `PATCH` — backwards-compatible fixes and additive migrations that need no
  operator action.

Migrations are forward-only. After an upgrade applies a migration, returning to
older code requires a matching database restore.

## Stable Release Procedure

### 1. Prepare the release pull request

Set the release version, generate a changelog draft, and run the local quality
checks from the release branch:

```bash
RELEASE_VERSION=X.Y.Z
RELEASE_TAG="v${RELEASE_VERSION}"
RELEASE_BRANCH="$(git branch --show-current)"
test -n "$RELEASE_BRANCH"
test "$RELEASE_BRANCH" != main

git cliff --tag "$RELEASE_TAG" -o /tmp/jarvis-changelog-draft.md
make check
```

Review the generated notes, update the versioned product files, and include
those changes in the pull request.

The pull request must pass the hosted CI aggregate, the strict Docs build, and
the `Security / Security gate` aggregate, which covers `pip-audit`, `npm-audit`,
`osv-scanner`, the Docker build smoke test, `gitleaks`, and CodeQL. Run the
independent hosted checks against the same branch:

```bash
gh workflow run nightly-llm-smoke.yml --ref "$RELEASE_BRANCH"
gh workflow run lifecycle-smoke.yml --ref "$RELEASE_BRANCH" -f leg=all \
  -f update_mode=direct
```

If the lifecycle run fails, download its `lifecycle-smoke-log` artifact before
anything else and search it for `=== diagnostics: project` — a run covering
every leg keeps going after one fails, so the failing leg's evidence is usually
far from the end of the log. The failure path in step 3 explains what that
block contains.

This branch check selects the two newest published tags, which predate the
candidate's bootstrap, so it runs in direct mode. The bootstrap paths are
covered by the per-source upgrade checks in step 3, which pass explicit
`update_from` and `update_to` values.

The nightly workflow's catalog-freshness job runs only on its schedule, so it
reports as skipped on this dispatch; that skip is expected and is not part of
the release acceptance.

Required JUnit selections must contain at least one pass and no skips, failures,
or errors. The lifecycle run must complete its CA-verified HTTPS and destructive
restore checks. These are public-repository workflows; never redirect them to a
self-hosted runner.

### 2. Merge and identify the exact release commit

Squash-merge the pull request after every required hosted check succeeds. Then
refresh the protected checkout and record the commit eligible for publication:

```bash
git switch main
git pull --ff-only
MERGED_SHA="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$MERGED_SHA" origin/main
```

The publication workflow also enforces that `MERGED_SHA` equals the dispatched
`github.sha` and is main-reachable.

### 3. Verify commit-addressed images

Dispatch the existing GHCR workflow from `main`. A dispatch without
`source_commit` is build-only; this invocation publishes commit-addressed
`<MERGED_SHA>` and `<MERGED_SHA>-cuda` verification manifests:

```bash
gh workflow run ghcr-publish.yml --ref main -f source_commit="$MERGED_SHA"
```

Wait for every build, manifest, SBOM, and vulnerability-report job to finish.
Record that run's ID and manifest-digest artifacts. Stop here if any job fails.
The checks below pull the SHA images, so dispatching them before this run
finishes fails every one of them with `manifest unknown`:

```bash
VERIFY_RUN_ID="$(gh run list --workflow=ghcr-publish.yml --limit 1 \
  --json databaseId --jq '.[0].databaseId')"
gh run watch "$VERIFY_RUN_ID"
```

Each build leg whose image carries a Python interpreter also runs an import
check inside the image it just built: the image's own interpreter imports the
service's entry module, so a missing runtime dependency fails the leg before
its digest can join a manifest. The dashboard image is nginx, carries no
interpreter, and has no import target. This proves the dependency set only.
That the containers actually start, read their configuration, and report
healthy is proven by the cold-install and upgrade checks below, before any
stable tag exists.

Only after that run succeeds, use the SHA images for the credential-free install
and supported upgrade checks:

```bash
gh workflow run first-run-smoke.yml --ref main \
  -f cold_install_version="$MERGED_SHA"

UPDATE_FROM=vA.B.C
# Per the table below: bootstrap for the older sources, direct for the newest.
UPDATE_MODE=bootstrap
gh workflow run lifecycle-smoke.yml --ref main -f leg=update \
  -f update_from="$UPDATE_FROM" -f update_to="$MERGED_SHA" \
  -f update_mode="$UPDATE_MODE"
```

When a lifecycle run fails, start from its `lifecycle-smoke-log` artifact rather
than the job page. For a failed leg that owns a Compose project — `tls`,
`update`, or `uninstall` — search the log for `=== diagnostics: project`, which
opens that project's container listing, each container's state and exit code,
and each container's own output, captured before the leg's resources were
removed. A run covering every leg continues after a leg fails, so that block
will not be at the end of the log. The smoke registers no Compose project for
the `restore` leg, so it produces no such block; its evidence is the round-trip
sub-script log, which the smoke echoes inline, and which names the separate
project the sub-script creates for its fixture. Fix the cause, then re-dispatch
only the check that failed:
identical re-dispatches supersede each other, while runs differing in `leg`,
`update_from`, `update_to`, or `update_mode` now run concurrently.

Run the upgrade check for each maintained source contract:

| Source release | Update path | Interrupted-update state |
|---|---|---|
| `v1.2.0` | `bootstrap` | `current-merge-pending` |
| `v1.2.1` | `bootstrap` | `current-merge-pending` |
| `v1.2.2` | `bootstrap` | `current-merge-pending` |
| `v1.2.3` | `bootstrap` | `current-merge-pending` |
| `v1.2.4` | `direct` | `current-merge-pending` |

The `direct` row is the path essentially every existing installation takes, and
it exercises the update transaction itself rather than the bootstrap. Do not skip
it because the bootstrap rows passed: they enter through different code, and a
release that changes the update transaction is only covered by this row.

The table encodes a rule: the newest published release updates `direct`, because
it already ships the update transaction the candidate builds on, and every older
supported source enters through the bootstrap. It names explicit tags rather than
deriving them, so refresh it while preparing each release — the release being
published becomes the new `direct` row, the previous `direct` row becomes a
`bootstrap` row, and any source that has left support is dropped.

These checks enforce three separate compatibility floors. Maintained in-place
updates start at v1.2.0. The immutable v1.2.2 bootstrap remains documented as a
separate legacy bridge from v1.1.3, but v1.1.3 is not a maintained source row and
direct v1.1.3-to-current updates are not supported. Portable fresh-host restore
starts with complete, signed backup sets created by v1.2.0 or later. Earlier or
unsigned sets retain only the constrained same-host recovery paths described in
the backup guide; they are not universally portable.

The supported window is deliberate: the `bootstrap` rows reach back at most
four releases behind the `direct` row. When adding a release to the table
would leave more than four `bootstrap` rows, the oldest row leaves support
and the release notes say so. An installation older than every table row
completes the documented one-time step in the
[command-line reference](manual/cli.md#updating-from-a-release-before-v122)
and then follows the bootstrap path; the floor below that is a fresh install
plus a backup restore.

The 40-hex value selects commit-addressed verification images; it is not a Git
tag, version, prerelease, or GitHub Release. The cold install must pull
anonymously, build no application image, reach a healthy stack, and remove its
isolated project resources. Each upgrade must start at the selected stable tag,
recover from its supported interrupted-update state, finish at `MERGED_SHA`,
and leave no pending journal or project resource behind.
The `bootstrap` rows load the candidate's bootstrap before invoking the updater.
The `direct` row does not: it runs the source release's own installed command, so
it is the only check that exercises the update transaction the candidate ships.
Dispatch it with `UPDATE_MODE=direct`; running every row in bootstrap mode leaves
that transaction unverified.

### 4. Tag the release and promote exact digests

After the SHA-based checks pass, record that exact successful workflow run in
the annotated tag:

```bash
VERIFY_RUN_ID="replace-with-successful-run-id"
test "$(gh run view "$VERIFY_RUN_ID" --json headSha --jq .headSha)" = "$MERGED_SHA"
test "$(gh run view "$VERIFY_RUN_ID" --json conclusion --jq .conclusion)" = success

TAG_MESSAGE="$(mktemp)"
printf 'Release %s\n\nVerification-Run-ID: %s\n' \
  "$RELEASE_TAG" "$VERIFY_RUN_ID" > "$TAG_MESSAGE"

git tag -a "$RELEASE_TAG" "$MERGED_SHA" -F "$TAG_MESSAGE"
git push origin "$RELEASE_TAG"
```

Never point a stable tag at a commit that is not on `main`. The tag workflow
rejects non-stable `v*` names, peels the annotated tag to its commit, checks
that commit against `origin/main`, and validates the recorded run through the
GitHub Actions API. The run must be a completed, successful dispatch of the
GHCR workflow from `main` for the tagged commit. Stable mode cannot build
images. Each promotion job downloads its named digest receipt from that exact
run, requires one well-formed receipt, promotes the content-addressed digest to
`${RELEASE_VERSION}` or `${RELEASE_VERSION}-cuda`, fails if the manifest is
absent or the resulting digest differs, and never moves `latest`. It does not
re-resolve the mutable SHA tags.

Confirm every stable manifest digest equals its SHA-tagged source digest. A
failed or cancelled promotion stops the release.

### 5. Create the GitHub Release

After digest promotion succeeds, publish the GitHub Release from the existing
stable tag:

```bash
gh release create "$RELEASE_TAG" --verify-tag \
  --title "$RELEASE_TAG" \
  --notes-file /tmp/jarvis-release-notes.md
```

The title is the tag alone. The notes file is a written release note, not the
generated changelog draft from step 1 and not a copy of the `CHANGELOG.md`
section; open the two most recent releases and match them. The shape is a
`## vX.Y.Z — <short summary>` heading, one paragraph of intent, a line stating
which migrations the release carries and whether they need operator action, a
line stating whether the images differ from the previous tag, condensed
`### Added` / `### Fixed` / `### Changed` sections, an `### Upgrading` section
giving the direct and bootstrap paths from each supported source, and a closing
`**Full changelog:**` link to `CHANGELOG.md` at this tag.

A release that retires an upgrade source from the support table states that in
its `### Upgrading` section.

## Release Checks

| Class | Required path | Acceptance rule |
|---|---|---|
| Local and pull request | Run `make check`; require hosted CI, strict Docs, and Security checks on the release pull request. | Every required job succeeds. |
| Independent integration | Dispatch the nightly Qdrant workflow and all lifecycle legs on `$RELEASE_BRANCH`. | Every required selection has passes and no skips, failures, or errors. |
| Exact commit publication | Dispatch GHCR with `source_commit="$MERGED_SHA"`. | All SHA manifests, SBOMs, reports, and digest receipts succeed under the `release` environment. |
| Install and upgrade | Run the anonymous SHA cold install and upgrade each supported source release to the same SHA. | The pull-only install and every resumable upgrade pass without leaving project resources behind. |
| Stable publication | Put the successful verification run ID in the annotated tag, push it, verify digest-preserving promotion from that run's artifacts, then create the GitHub Release. | The run matches the tagged `main` commit, every stable digest matches its exact receipt, and no `latest` mutation occurs. |

## Dependency Scans

`pip-audit`, `npm-audit`, and `osv-scanner` evaluate a live advisory database
against a static lockfile, so a scan that passed an hour ago can fail with no
change to the branch. A newly published advisory is the expected cause of a
dependency job turning red mid-release; check when it was published before
looking for a regression in the diff:

```bash
gh api /advisories/<GHSA-ID> --jq '{summary, published_at, vulnerabilities}'
```

Patch whenever the fixed version is installable, and reserve a dated suppression
for an advisory that has none. Raising a floor means raising it in every manifest
that declares it — the root `pyproject.toml`, `libs/jarvis_common/pyproject.toml`,
and each service's `requirements.txt` — then re-locking both projects and
regenerating the lock-derived pins with `scripts/export-service-requirements.sh`.

Reproduce the locally runnable dependency and secret scans before pushing a
release branch and again before tagging:

```bash
make security-scan
```

The target pins the same osv-scanner and gitleaks artifacts as the hosted
workflow, keeps them outside the repository, and verifies both downloaded
artifacts and executable bytes on every run. It also runs the three pinned
Python dependency inputs and the checked npm audit policy. It supports Linux
x86_64; on another workstation, use the hosted Security workflow. A local pass
does not replace the hosted Security aggregate or CodeQL evidence.

## Changelog Generation

`CHANGELOG.md` is written by hand, in user-facing prose grouped under `### Added`,
`### Fixed`, and `### Changed`. [git-cliff](https://github.com/orhun/git-cliff),
configured in `cliff.toml`, produces a commit-derived draft that seeds that
writing; it is not wired into CI, and its output is not committed verbatim.
Conventional commit prefixes map to draft sections:

| Commit prefix | CHANGELOG section |
|---|---|
| `feat` | Features |
| `fix` | Bug Fixes |
| `perf` | Performance |
| `refactor` | Refactoring |
| `security` | Security |
| `docs` | Documentation |
| `test` | Testing |
| `chore` | Miscellaneous Tasks |

Breaking changes with `BREAKING:` in the body are highlighted. Merge commits and
reverts are omitted.

## Docker Image Versioning

The GHCR workflow publishes
`ghcr.io/limitcycle-oss/jarvis-{paper-ingestion,learning-engine,telegram-bot,dashboard,restore-uploader}`.
Paper ingestion also has a CUDA flavor. `langfuse-hardened` remains a local
observability-only build.

Commit verification tags are the lowercase 40-hex Git commit, with `-cuda` for
the CUDA flavor. Stable image tags omit Git's leading `v`; a release tag
`vX.Y.Z` therefore promotes to `:X.Y.Z` and `:X.Y.Z-cuda`. Promotion reuses
registry digests and never rebuilds.

To run an already published stable version:

```bash
JARVIS_IMAGE_TAG=X.Y.Z docker compose pull
JARVIS_IMAGE_TAG=X.Y.Z docker compose up -d --no-build
```

`JARVIS_VERSION` records the semantic application version represented by the
checkout. `JARVIS_IMAGE_TAG` selects the published application images, including
commit-addressed verification images used before a stable tag exists.

The `build:` blocks remain available to contributors through
`./setup.sh --build-local` and `./update.sh --build-local`.

## Rollback Procedures

### Code Rollback

> **Database warning:** Moving the source checkout back without a matching
> database restore can leave the schema ahead of the code. Coordinate a code
> rollback with a restore point, or use a forward-only corrective migration.

To return to a previously published release after confirming its schema is
compatible:

```bash
JARVIS_IMAGE_TAG=<previous-version> docker compose pull
JARVIS_IMAGE_TAG=<previous-version> docker compose up -d --no-build
```

If the target predates published images, or the installation uses local builds,
check out the target tag and rebuild instead.

### Database Rollback

Database migrations are intentionally forward-only. To revert a schema change:

1. Restore a matching encrypted backup. See
   [Backup and restore](manual/backup-and-restore.md).
2. If an in-place correction is required, add a new forward migration that
   restores the intended schema.

Review migration diffs before upgrading and test them against a copy of
production data.
