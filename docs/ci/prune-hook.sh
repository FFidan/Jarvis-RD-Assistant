#!/usr/bin/env bash
# Self-hosted runner post-job hook — keeps Docker disk bounded on the runner host.
#
# Canonical version. Wired on each runner via ACTIONS_RUNNER_HOOK_JOB_COMPLETED
# in the runner's .env, so it runs after EVERY job. Apply with (RUNNER_BASE_DIR
# is wherever your self-hosted runners are installed):
#   cp docs/ci/prune-hook.sh "${RUNNER_BASE_DIR:-/opt/actions-runners}/<runner-name>/prune-hook.sh"
# Repeat for each runner instance registered on the host.
#
# Best-effort only: never fails a job.
#
# Serialized box-wide via flock so a prune never overlaps another runner's job
# docker ops. That overlap was a contributor to CI-CROSS-USER-FLAKY-1 daemon
# contention; with two runners sharing one Docker daemon, an unsynchronized
# prune mid-build/-run could time out a sibling job's docker call. `-n` = skip
# (do not wait) if another prune already holds the lock — pruning can wait for
# the next job rather than block.
#
# Removes:
#   - dangling (untagged) images OLDER THAN 1h — layer churn from rebuilds
#   - build cache unused for >1 week (recent cache is kept so builds stay fast)
# Never touches: tagged images, volumes, containers, recent build cache, or
# dangling images <1h old.
#
# The `until=1h` on the image prune is load-bearing for the 2-runner setup: the
# legacy Docker builder (DOCKER_BUILDKIT=0 in docker-build-smoke) leaves each
# multi-stage intermediate (e.g. the `jarvis-common-builder` stage) as a
# DANGLING image while the build is in flight. Without the time filter, a prune
# fired by one runner's post-job hook removes the OTHER runner's in-flight
# intermediate mid-build → `COPY --from=jarvis-common-builder: No such image`.
# Keeping <1h dangling images protects every in-flight build (builds take minutes).
exec 9>/tmp/jarvis-docker-prune.lock
flock -n 9 || exit 0
docker image prune -f --filter "until=1h" >/dev/null 2>&1 || true
docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
exit 0
