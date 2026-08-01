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

The pull request must pass the hosted CI aggregate, strict Docs build, and
`Security / npm-audit`. Run the independent hosted checks against the same
branch:

```bash
gh workflow run nightly-llm-smoke.yml --ref "$RELEASE_BRANCH"
gh workflow run lifecycle-smoke.yml --ref "$RELEASE_BRANCH" -f leg=all \
  -f update_mode=direct
```

If the lifecycle run fails, download its `lifecycle-smoke-log` artifact before
anything else and read the tail; the failure path in step 3 explains what that
log contains.

This branch check selects the two newest published tags, which predate the
candidate's bootstrap, so it runs in direct mode. The bootstrap paths are
covered by the per-source upgrade checks in step 3, which pass explicit
`update_from` and `update_to` values.

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

Use the SHA images for the credential-free install and supported upgrade
checks:

```bash
gh workflow run first-run-smoke.yml --ref main \
  -f cold_install_version="$MERGED_SHA"

UPDATE_FROM=vA.B.C
UPDATE_MODE=bootstrap
gh workflow run lifecycle-smoke.yml --ref main -f leg=update \
  -f update_from="$UPDATE_FROM" -f update_to="$MERGED_SHA" \
  -f update_mode="$UPDATE_MODE"
```

When a lifecycle run fails, start from its `lifecycle-smoke-log` artifact rather
than the job page. For a failed leg that owns a Compose project — `tls`,
`update`, or `uninstall` — the log tail carries that project's container
listing, each container's state and exit code, and each container's own output,
captured before the leg's resources were removed. The `restore` leg owns no
Compose project; its evidence is the round-trip sub-script log, which the smoke
echoes inline. Fix the cause, then re-dispatch only the check that failed:
identical re-dispatches supersede each other, while runs differing in `leg`,
`update_from`, `update_to`, or `update_mode` now run concurrently.

Run the upgrade check for each maintained source contract:

| Source release | Update path | Interrupted-update state |
|---|---|---|
| `v1.1.3` | `bootstrap` | `current-merge-pending` |
| `v1.2.0` | `bootstrap` | `current-merge-pending` |
| `v1.2.1` | `bootstrap` | `current-merge-pending` |

The 40-hex value selects commit-addressed verification images; it is not a Git
tag, version, prerelease, or GitHub Release. The cold install must pull
anonymously, build no application image, reach a healthy stack, and remove its
isolated project resources. Each upgrade must start at the selected stable tag,
recover from its supported interrupted-update state, finish at `MERGED_SHA`,
and leave no pending journal or project resource behind.
Every source loads the candidate's bootstrap before invoking the updater, so
each check exercises the update path the candidate actually ships.

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
  --title "JARVIS RD Assistant ${RELEASE_TAG}" \
  --notes-file /tmp/jarvis-changelog-draft.md
```

## Release Checks

| Class | Required path | Acceptance rule |
|---|---|---|
| Local and pull request | Run `make check`; require hosted CI, strict Docs, and Security checks on the release pull request. | Every required job succeeds. |
| Independent integration | Dispatch the nightly Qdrant workflow and all lifecycle legs on `$RELEASE_BRANCH`. | Every required selection has passes and no skips, failures, or errors. |
| Exact commit publication | Dispatch GHCR with `source_commit="$MERGED_SHA"`. | All SHA manifests, SBOMs, reports, and digest receipts succeed under the `release` environment. |
| Install and upgrade | Run the anonymous SHA cold install and upgrade each supported source release to the same SHA. | The pull-only install and every resumable upgrade pass without leaving project resources behind. |
| Stable publication | Put the successful verification run ID in the annotated tag, push it, verify digest-preserving promotion from that run's artifacts, then create the GitHub Release. | The run matches the tagged `main` commit, every stable digest matches its exact receipt, and no `latest` mutation occurs. |

## Changelog Generation

`CHANGELOG.md` is generated by [git-cliff](https://github.com/orhun/git-cliff),
configured in `cliff.toml`. Conventional commit prefixes map to sections:

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
