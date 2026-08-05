#!/usr/bin/env bash
# test_update_coverage.sh — behavioral tests for update.sh and the shared release
# helpers in scripts/setup_lib.sh. No docker daemon or network is needed: docker
# and git are stubbed on a private PATH (the pattern established by
# scripts/tests/test_setup_lib_helpers.sh's fake_docker), and update.sh itself is
# run against a throwaway fixture repo whose versions.env pins differ from what
# the docker stub reports running.
#
# Coverage:
#   * print_split_recovery prints only the half (versions.env vs JARVIS_IMAGE_TAG)
#     that matches the failed set, and never names a third-party pin in the
#     JARVIS_IMAGE_TAG rollback line;
#   * update.sh --yes runs promptless; all image pulls complete before any
#     container is recreated; a pull failure prints the split recovery and exits
#     1 with nothing recreated; a no-healthcheck service is reported, not silently
#     counted as verified; checkout metadata overrides a stale .env application
#     pin, including an exact release-candidate tag, and the new pin is persisted
#     only after successful health checks;
#   * latest_stable_tag excludes pre-releases and sorts versionally;
#   * install_cli_shim is idempotent and prepend-dedups the installs registry;
#   * verify_release_manifests strips the tag's v-prefix from image refs, passes
#     only when every inspected image is present, and skips local-build images.
#
# Run: bash scripts/tests/test_update_coverage.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UPDATE_SCRIPT="${REPO_ROOT}/update.sh"
LIB="${REPO_ROOT}/scripts/setup_lib.sh"
LIFECYCLE_HELPER="${REPO_ROOT}/scripts/backup-lifecycle.sh"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
check_fail() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }

# The shared helpers under test, plus _env_key_in_list which print_split_recovery
# consumes to classify a failed service.
# shellcheck source=../setup_lib.sh
# shellcheck disable=SC1091
source "$LIB"

# =============================================================================
# print_split_recovery — extracted from update.sh and evaluated here, so the one
# implementation is what is tested (a private copy would drift).
# =============================================================================
# C_BOLD/C_RESET are consumed by the eval'd print_split_recovery, invisibly to shellcheck.
# shellcheck disable=SC2034
C_BOLD=""
# shellcheck disable=SC2034
C_RESET=""
psr_src="$(sed -n '/^print_split_recovery() {/,/^}/p' "$UPDATE_SCRIPT")"
if [ -z "$psr_src" ]; then
  printf 'FAIL: could not sed-extract print_split_recovery from %s\n' "$UPDATE_SCRIPT" >&2
  exit 1
fi
eval "$psr_src"

# Representative third-party services for split-recovery classification,
# including the optional cloudflared edge.
TP_SET="postgres ollama qdrant litellm cloudflared postgres-backup"
PREVIOUS_IMAGE_TAG=1.1.3
SCRIPT_DIR=/srv/jarvis-family
BUILD_LOCAL=0
APP_PROFILE_ARGS=(--profile telegram --profile tunnel)
run_recovery() { THIRD_PARTY_SET="$TP_SET" print_split_recovery "$@"; }
has()  { printf '%s' "$1" | grep -q "$2"; }
want() { if has "$1" "$2"; then pass "$3"; else check_fail "$3 ($1)"; fi; }
lack() { if has "$1" "$2"; then check_fail "$3 ($1)"; else pass "$3"; fi; }

# --- app-only: FAILED=(dashboard) --------------------------------------------
out="$(run_recovery dashboard)"
lack "$out" 'Third-party services' "app-only: no third-party/versions.env block"
want "$out" 'Application services'  "app-only: prints the application/JARVIS_IMAGE_TAG block"
want "$out" 'Application-image recovery (not a full release rollback)' \
  "app-only: labels the bounded recovery honestly"
want "$out" 'Repository: /srv/jarvis-family' \
  "app-only: names the repository where commands must run"
want "$out" 'JARVIS_IMAGE_TAG=1.1.3 docker compose --profile telegram --profile tunnel pull dashboard' \
  "app-only: exact previous pin and active profiles reach the app service"
lack "$out" '<previous-version>' "app-only: never prints a placeholder version"
want "$out" 'do not move the Git checkout or restore stored data' \
  "app-only: scopes image recovery away from Git and data"
want "$out" 'docker compose logs --tail=200 dashboard' "app-only: trailing logs line lists the full set"

# --- third-party-only: FAILED=(postgres) -------------------------------------
out="$(run_recovery postgres)"
want "$out" 'Third-party services' "third-party-only: prints the third-party/versions.env block"
lack "$out" 'Application services' "third-party-only: no application/JARVIS_IMAGE_TAG block"
lack "$out" 'JARVIS_IMAGE_TAG='    "third-party-only: no JARVIS_IMAGE_TAG line at all"
want "$out" 'cd /srv/jarvis-family' "third-party-only: recovery command is scoped to its repository"

# --- reconciled third-party (postgres-backup) classifies as third-party -------
out="$(run_recovery postgres-backup)"
if has "$out" 'Third-party services' && ! has "$out" 'Application services'; then
  pass "postgres-backup classifies as third-party, not application"
else
  check_fail "postgres-backup misclassified ($out)"
fi

# --- mixed: FAILED=(dashboard postgres) --------------------------------------
out="$(run_recovery dashboard postgres)"
if has "$out" 'Third-party services' && has "$out" 'Application services'; then
  pass "mixed: prints both blocks"
else
  check_fail "mixed: missing one block ($out)"
fi
version_lines="$(printf '%s\n' "$out" | grep 'JARVIS_IMAGE_TAG=')"
if [ -n "$version_lines" ] && ! printf '%s\n' "$version_lines" \
    | grep -qE 'postgres|ollama|qdrant|litellm|cloudflared'; then
  pass "mixed: JARVIS_IMAGE_TAG lines never name a third-party service"
else
  check_fail "mixed: JARVIS_IMAGE_TAG line named a third-party service ($version_lines)"
fi
want "$out" 'docker compose logs --tail=200 dashboard postgres' \
  "mixed: trailing logs line lists the full (both) failed set"

# If the persisted pin is unavailable, recovery must stop short of inventing an
# executable version placeholder.
PREVIOUS_IMAGE_TAG=""
out="$(run_recovery dashboard)"
lack "$out" 'JARVIS_IMAGE_TAG=' "missing old pin: no speculative image command"
lack "$out" '<previous-version>' "missing old pin: no placeholder version"
want "$out" 'could not be read safely from .env' \
  "missing old pin: explains why no image command was printed"
PREVIOUS_IMAGE_TAG=1.1.3

# --build-local does not promise that an old registry image exists. It may only
# re-use a previous local image when that exact tag is still cached.
BUILD_LOCAL=1
out="$(run_recovery dashboard)"
lack "$out" 'docker compose --profile telegram --profile tunnel pull dashboard' \
  "local-build recovery: does not pull a potentially unpublished app tag"
want "$out" 'still cached locally' \
  "local-build recovery: states the cache precondition"
BUILD_LOCAL=0

# The command-line help must not call source builds air-gapped: base images and
# package/build inputs still have to exist locally or remain reachable.
out="$(bash "$UPDATE_SCRIPT" --help)"; rc=$?
if [ "$rc" -eq 0 ]; then pass "update help exits 0"; else check_fail "update help exits rc=$rc"; fi
lack "$out" 'air-gapped' "update help: does not promise an air-gapped build"
want "$out" 'base images and build inputs must be cached or reachable' \
  "update help: states the actual source-build prerequisite"

# fail_with_recovery exits 1 after printing the split recovery.
fwr_src="$(sed -n '/^fail_with_recovery() {/,/^}/p' "$UPDATE_SCRIPT")"
# shellcheck disable=SC2329  # err is called indirectly by the eval'd fail_with_recovery
( eval "$fwr_src"; err() { :; }; THIRD_PARTY_SET="$TP_SET" \
    fail_with_recovery "boom" "hint" dashboard >/dev/null 2>&1 ); rc=$?
if [ "$rc" -eq 1 ]; then pass "fail_with_recovery exits 1"; else check_fail "fail_with_recovery did not exit 1 (rc=$rc)"; fi
out="$(
  eval "$fwr_src"
  err() { :; }
  UPDATE_MANAGED_TRANSACTION=1
  THIRD_PARTY_SET="$TP_SET"
  fail_with_recovery "boom" "hint" dashboard 2>&1
)"; rc=$?
if [ "$rc" -eq 1 ] && ! has "$out" 'Application-image recovery'; then
  pass "managed transaction defers recovery to the lifecycle CLI"
else
  check_fail "managed transaction duplicated direct recovery: rc=$rc out=<<<$out>>>"
fi

DIRECT_UPDATE_CLEANUP_FN="$(sed -n '/^_cleanup_direct_update_lifecycle() {/,/^}/p' "$UPDATE_SCRIPT")"
for mutation_expected in "0 clear" "1 retain"; do
  mutation="${mutation_expected%% *}"
  expected="${mutation_expected#* }"
  out="$(
    set +e
    eval "$DIRECT_UPDATE_CLEANUP_FN"
    UPDATE_LIFECYCLE_OWNED=1
    UPDATE_MUTATION_STARTED="$mutation"
    SCRIPT_DIR=/tmp/update-fixture
    finish_lifecycle_operation() { printf '%s\n' "$3"; }
    false
    _cleanup_direct_update_lifecycle
  )" && rc=0 || rc=$?
  if [ "$out" = "$expected" ] && [ "$rc" -eq 1 ]; then
    pass "direct update failed lifecycle exit uses ${expected} after mutation=${mutation}"
  else
    check_fail "direct update lifecycle cleanup: mutation=$mutation action=$out rc=$rc"
  fi
done

# =============================================================================
# update.sh full-run harness (stubbed docker + throwaway fixture repo).
# =============================================================================
FX="$(mktemp -d)"
STUB="$(mktemp -d)"
trap 'rm -rf "$FX" "$STUB"' EXIT
FAST_SLEEP_BIN="$FX/fast-sleep-bin"
REAL_SLEEP_BIN="$(command -v sleep)"
mkdir -p "$FAST_SLEEP_BIN"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [ "${1:-}" = 3 ]; then exit 0; fi' \
  'exec "$JARVIS_TEST_REAL_SLEEP" "$@"' \
  > "$FAST_SLEEP_BIN/sleep"
chmod +x "$FAST_SLEEP_BIN/sleep"

ln -s "$UPDATE_SCRIPT" "$FX/update.sh"
mkdir -p "$FX/scripts"
ln -s "$LIB" "$FX/scripts/setup_lib.sh"
cp "$LIFECYCLE_HELPER" "$FX/scripts/backup-lifecycle.sh"
# init-secrets.sh stub. It records its invocation in the SAME ordered log the
# docker stub writes to, which is what makes the ordering assertion below
# possible: position in one log is the only way to prove the secrets phase runs
# before the first pull/build/up. STUB_FAIL_SECRETS models a secret that cannot
# be written. The real script is idempotent and covered by its own boot path;
# reproducing it here would only test the stub.
cat > "$FX/scripts/init-secrets.sh" <<'SECRETS'
#!/usr/bin/env bash
printf 'init-secrets ensured\n' >> "$DOCKER_LOG"
[ "${STUB_FAIL_SECRETS:-0}" = 1 ] && exit 1
exit 0
SECRETS
chmod +x "$FX/scripts/init-secrets.sh"
printf 'services: {}\nvolumes:\n  postgres_backups:\n' > "$FX/docker-compose.yml"
cat > "$FX/pyproject.toml" <<'PYPROJECT'
[project]
name = "jarvis-rd-assistant"
version = "1.2.0"
PYPROJECT
# Pins deliberately unequal to the stub's reported running image, so every base
# third-party service diffs as "update available".
cat > "$FX/versions.env" <<'VERS'
POSTGRES_IMAGE=postgres:test-new
OLLAMA_IMAGE=ollama/ollama:test-new
QDRANT_IMAGE=qdrant/qdrant:test-new
LITELLM_IMAGE=litellm:test-new
CLOUDFLARED_IMAGE=cloudflare/cloudflared:test-new
CADDY_IMAGE=caddy:test-new
VECTOR_IMAGE=timberio/vector:test-new
LANGFUSE_POSTGRES_IMAGE=postgres:test-new-alpine
VERS
# TORCH_VARIANT present so the backfill is a no-op; no telegram token. The stale
# application pin models a manual git-pull upgrade from 1.1.3 to this checkout.
reset_fixture_env() {
  printf 'TORCH_VARIANT=cpu\nTORCH_VARIANT_SUFFIX=\nJARVIS_VERSION=1.1.3\nJARVIS_IMAGE_TAG=1.1.3\n' > "$FX/.env"
}
reset_fixture_env

cat > "$STUB/git" <<'GIT'
#!/usr/bin/env bash
if [ "${1:-}" = describe ] && [ -n "${STUB_EXACT_TAG:-}" ]; then
  printf '%s\n' "$STUB_EXACT_TAG"
  exit 0
fi
exit 1
GIT
chmod +x "$STUB/git"

# docker stub: logs pull/up/build to $DOCKER_LOG; reports the base third-party
# and application services as running with a stale image. Optional ingress
# services exist only when named in STUB_ACTIVE_INGRESS. A file-backed
# STUB_HEALTH_SEQUENCE supplies exact health|run-state samples; the scalar
# STUB_HEALTH and STUB_RUN_STATE values are the fallback.
cat > "$STUB/docker" <<'DOCKER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_CALL_LOG"
log() { printf '%s\n' "$*" >> "$DOCKER_LOG"; }
health_sample() {
  local sample="" pin
  if [ -n "${STUB_HEALTH_SEQUENCE:-}" ] && [ -f "$STUB_HEALTH_SEQUENCE" ]; then
    sample="$(sed -n '1p' "$STUB_HEALTH_SEQUENCE")"
    if [ -n "$sample" ]; then
      sed '1d' "$STUB_HEALTH_SEQUENCE" > "${STUB_HEALTH_SEQUENCE}.next"
      mv "${STUB_HEALTH_SEQUENCE}.next" "$STUB_HEALTH_SEQUENCE"
    fi
  fi
  if [ -z "$sample" ]; then
    sample="${STUB_HEALTH-healthy}|${STUB_RUN_STATE-running}"
  fi
  pin="$(sed -n 's/^JARVIS_VERSION=//p' "$STUB_REPO/.env" 2>/dev/null | head -1)"
  log "health sample=${sample} pin=${pin:-missing}"
  printf '%s\n' "$sample"
}
running_cid() {
  case "$1" in
    postgres|ollama|qdrant|litellm|postgres-backup) printf 'cid-%s\n' "$1" ;;
    paper_ingestion|learning_engine|dashboard|restore-uploader|telegram_bot) printf 'cid-%s\n' "$1" ;;
    caddy|caddy_local|cloudflared)
      case " ${STUB_ACTIVE_INGRESS:-} " in *" $1 "*) printf 'cid-%s\n' "$1" ;; esac ;;
    *) : ;;
  esac
}
case "${1:-}" in
  info) [ "${STUB_NO_DAEMON:-0}" = 1 ] && exit 1; exit 0 ;;
  volume)
    case "${2:-}" in
      inspect)
        if printf '%s\n' "$@" | grep -q -- '--format'; then
          project="$(basename "$STUB_REPO" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
          printf '%s|postgres_backups\n' "$project"
        fi
        exit 0 ;;
    esac
    exit 0 ;;
  run)
    raw_args=("$@")
    for ((i=0; i<${#raw_args[@]}; i++)); do
      if [ "${raw_args[$i]}" = /tmp/backup-lifecycle.sh ]; then
        helper_args=("${raw_args[@]:$((i + 1))}")
        if [ "${helper_args[0]:-}" = update-promoted-status ] \
           && [ "${STUB_DEAD_GUARD:-0}" != 1 ]; then
          exit 0
        fi
        exit 1
      fi
    done
    exit 0 ;;
esac
if [ "${1:-}" = "inspect" ]; then
  shift; fmt=""
  while [ $# -gt 0 ]; do
    case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
  done
  case "$fmt" in
    *State.Health*State.Status*) health_sample ;;
    *Config.Image*) printf 'oldimage:running\n' ;;
    *State.Health*) printf '%s\n' "${STUB_HEALTH-healthy}" ;;
    *State.Status*) printf '%s\n' "${STUB_RUN_STATE-running}" ;;
  esac
  exit 0
fi
if [ "${1:-}" = "compose" ]; then
  printf 'compose-env file=%s project=%s profiles=%s separator=%s envfiles=%s disable=%s\n' \
    "${COMPOSE_FILE-<unset>}" "${COMPOSE_PROJECT_NAME-<unset>}" \
    "${COMPOSE_PROFILES-<unset>}" "${COMPOSE_PATH_SEPARATOR-<unset>}" \
    "${COMPOSE_ENV_FILES-<unset>}" "${COMPOSE_DISABLE_ENV_FILE-<unset>}" \
    >> "$DOCKER_CALL_LOG"
  shift; args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile|--project-directory|--env-file|-p|-f) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  set -- "${args[@]:-}"
  case "${1:-}" in
    version) exit 0 ;;
    config)
      project="$(basename "$STUB_REPO" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
      printf '{"volumes":{"postgres_backups":{"name":"%s_postgres_backups"}}}\n' "$project"
      exit 0 ;;
    ps)      running_cid "${3:-}"; exit 0 ;;
    pull)
      log "pull ${*:2} JARVIS_VERSION=${JARVIS_VERSION:-<unset>} JARVIS_IMAGE_TAG=${JARVIS_IMAGE_TAG:-<unset>}"
      [ "${STUB_FAIL_PULL:-0}" = 1 ] && exit 1
      if [ -n "${STUB_FAIL_PULL_MATCH:-}" ] \
         && printf '%s\n' "$*" | grep -qF -- "$STUB_FAIL_PULL_MATCH"; then
        exit 1
      fi
      exit 0 ;;
    up)      log "network-up DASHBOARD_IP=${JARVIS_DASHBOARD_IP:-<unset>}"; log "up ${*:2} JARVIS_VERSION=${JARVIS_VERSION:-<unset>} JARVIS_IMAGE_TAG=${JARVIS_IMAGE_TAG:-<unset>}"; exit 0 ;;
    build)   log "build ${*:2} JARVIS_VERSION=${JARVIS_VERSION:-<unset>} JARVIS_IMAGE_TAG=${JARVIS_IMAGE_TAG:-<unset>}"; exit 0 ;;
    *)       exit 0 ;;
  esac
fi
exit 0
DOCKER
chmod +x "$STUB/docker"

run_update() {
  local stdin_data="${STUB_UPDATE_INPUT:-}"
  : > "$FX/docker.log"
  : > "$FX/docker-calls.log"
  mkdir -p "$FX/home"
  DOCKER_LOG="$FX/docker.log" DOCKER_CALL_LOG="$FX/docker-calls.log" \
    STUB_EXACT_TAG="${STUB_EXACT_TAG:-}" STUB_FAIL_PULL="${STUB_FAIL_PULL:-0}" \
    STUB_FAIL_PULL_MATCH="${STUB_FAIL_PULL_MATCH:-}" \
    STUB_ACTIVE_INGRESS="${STUB_ACTIVE_INGRESS:-}" \
    STUB_FAIL_SECRETS="${STUB_FAIL_SECRETS:-0}" \
    STUB_HEALTH="${STUB_HEALTH-healthy}" STUB_RUN_STATE="${STUB_RUN_STATE-running}" \
    STUB_HEALTH_SEQUENCE="${STUB_HEALTH_SEQUENCE:-}" \
    STUB_DEAD_GUARD="${STUB_DEAD_GUARD:-0}" STUB_NO_DAEMON="${STUB_NO_DAEMON:-0}" \
    STUB_REPO="$FX" \
    JARVIS_UPDATE_VOLUME_GUARD_ID="${JARVIS_UPDATE_VOLUME_GUARD_ID:-0123456789abcdef0123456789abcdef}" \
    HOME="$FX/home" XDG_CONFIG_HOME="$FX/home/.config" \
    JARVIS_TEST_REAL_SLEEP="$REAL_SLEEP_BIN" \
    PATH="$FAST_SLEEP_BIN:$STUB:$PATH" bash "$FX/update.sh" "$@" <<<"$stdin_data" 2>&1
}

# Durable update text is not proof that its detached sidecar lease is alive.
# The preflight may query Docker, but a dead holder must refuse before any
# service or configuration mutation.
reset_fixture_env
dead_guard_id=0123456789abcdef0123456789abcdef
before_env="$(cat "$FX/.env")"
out="$(STUB_DEAD_GUARD=1 JARVIS_UPDATE_VOLUME_GUARD_ID="$dead_guard_id" run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && ! grep -Eq '(^| )(pull|up|build)( |$)' "$FX/docker.log" \
   && [ "$(cat "$FX/.env")" = "$before_env" ]; then
  pass "dead inherited update lease refuses before service or config mutation"
else
  check_fail "dead inherited update lease escaped admission: rc=$rc calls=$(cat "$FX/docker-calls.log") out=<<<$out>>>"
fi

# A stopped daemon is diagnosed before the Docker-backed lifecycle helper runs.
reset_fixture_env
before_env="$(cat "$FX/.env")"
out="$(STUB_NO_DAEMON=1 run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'Docker daemon is not reachable' \
   && ! grep -q '/tmp/backup-lifecycle.sh' "$FX/docker-calls.log" \
   && [ "$(cat "$FX/.env")" = "$before_env" ]; then
  pass "direct update reports a stopped Docker daemon before lifecycle admission"
else
  check_fail "direct update daemon ordering: rc=$rc calls=$(cat "$FX/docker-calls.log") out=<<<$out>>>"
fi

# --- checkout_version_overrides_stale_env_and_commits_after_health ----------
reset_fixture_env
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && grep -Eq '^pull .*dashboard.*JARVIS_VERSION=1\.2\.0 JARVIS_IMAGE_TAG=1\.2\.0$' "$FX/docker.log" \
   && grep -Eq '^up .*dashboard.*JARVIS_VERSION=1\.2\.0 JARVIS_IMAGE_TAG=1\.2\.0$' "$FX/docker.log" \
   && grep -qx 'JARVIS_VERSION=1.2.0' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.2.0' "$FX/.env"; then
  pass "default update uses and persists the checkout version for both identities"
else
  check_fail "checkout_version_overrides_stale_env: rc=$rc env=$(cat "$FX/.env") log=$(cat "$FX/docker.log") out=$out"
fi

# A release smoke may select commit-addressed images while the checkout still
# reports its semantic application version.
reset_fixture_env
commit_image_tag=0123456789abcdef0123456789abcdef01234567
out="$(run_update --yes --image-tag "$commit_image_tag")"; rc=$?
if [ "$rc" -eq 0 ] \
   && grep -Eq "^pull .*dashboard.*JARVIS_VERSION=1\\.2\\.0 JARVIS_IMAGE_TAG=${commit_image_tag}$" "$FX/docker.log" \
   && grep -Eq "^up .*dashboard.*JARVIS_VERSION=1\\.2\\.0 JARVIS_IMAGE_TAG=${commit_image_tag}$" "$FX/docker.log" \
   && grep -qx 'JARVIS_VERSION=1.2.0' "$FX/.env" \
   && grep -qx "JARVIS_IMAGE_TAG=${commit_image_tag}" "$FX/.env"; then
  pass "commit image tag stays separate through pull, recreate, and persistence"
else
  check_fail "commit image identity: rc=$rc env=$(cat "$FX/.env") log=$(cat "$FX/docker.log") out=$out"
fi

# Invalid image selectors fail before any Docker request or persistent change.
reset_fixture_env
before_env="$(cat "$FX/.env")"
out="$(run_update --yes --image-tag 0123456789abcdef)"; rc=$?
if [ "$rc" -eq 1 ] && [ ! -s "$FX/docker-calls.log" ] \
   && [ "$(cat "$FX/.env")" = "$before_env" ] \
   && printf '%s' "$out" | grep -q 'Invalid --image-tag'; then
  pass "invalid image tag fails before Docker and persistence"
else
  check_fail "invalid image tag escaped validation: rc=$rc calls=$(cat "$FX/docker-calls.log") env=$(cat "$FX/.env") out=$out"
fi

# A recoverable unhealthy sample must consume another production poll before
# the application pin can commit.
reset_fixture_env
HEALTH_SEQUENCE="$FX/health-sequence"
printf '%s\n' 'unhealthy|running' 'healthy|running' > "$HEALTH_SEQUENCE"
out="$(STUB_HEALTH_SEQUENCE="$HEALTH_SEQUENCE" run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && grep -q '^health sample=unhealthy|running pin=1.1.3$' "$FX/docker.log" \
   && grep -q '^health sample=healthy|running pin=1.1.3$' "$FX/docker.log" \
   && grep -qx 'JARVIS_VERSION=1.2.0' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.2.0' "$FX/.env"; then
  pass "transient_unhealthy_converges_before_identity_commit"
else
  check_fail "transient health convergence: rc=$rc env=$(cat "$FX/.env") log=$(cat "$FX/docker.log") out=<<<$out>>>"
fi

# A permanently unhealthy service must exhaust all 180/3 production samples
# without persisting the checkout's application version.
reset_fixture_env
: > "$HEALTH_SEQUENCE"
for ((sample_i=0; sample_i<60; sample_i++)); do
  printf '%s\n' 'unhealthy|running' >> "$HEALTH_SEQUENCE"
done
out="$(STUB_HEALTH_SEQUENCE="$HEALTH_SEQUENCE" run_update --yes)"; rc=$?
unhealthy_samples="$(grep -c '^health sample=unhealthy|running pin=1.1.3$' "$FX/docker.log" || true)"
if [ "$rc" -eq 1 ] && [ "$unhealthy_samples" -eq 60 ] \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.3' "$FX/.env" \
   && printf '%s' "$out" | grep -q 'did not become healthy within 180s'; then
  pass "permanent_unhealthy_exhausts_budget_without_pin_commit"
else
  check_fail "permanent unhealthy gate: rc=$rc samples=$unhealthy_samples env=$(cat "$FX/.env") out=<<<$out>>>"
fi

# Caller-exported Compose selectors must never redirect a direct update away
# from the fixture repository. The repo's own .env remains Compose's source.
reset_fixture_env
export COMPOSE_FILE=/tmp/foreign-compose.yml
export COMPOSE_PROJECT_NAME=foreign-project
export COMPOSE_PROFILES=foreign-profile
export COMPOSE_PATH_SEPARATOR=';'
export COMPOSE_ENV_FILES=/tmp/foreign.env
export COMPOSE_DISABLE_ENV_FILE=1
out="$(run_update --yes)"; rc=$?
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR
unset COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE
if [ "$rc" -eq 0 ] \
   && grep -qF 'compose-env file=<unset> project=<unset> profiles=<unset> separator=<unset> envfiles=<unset> disable=<unset>' "$FX/docker-calls.log"; then
  pass "direct update clears caller Compose selectors before every Docker Compose call"
else
  check_fail "direct update leaked caller Compose selectors: rc=$rc calls=$(cat "$FX/docker-calls.log") out=<<<$out>>>"
fi

# A default install has no cloudflared container. It must stay out of the status
# table and update plan instead of appearing as a red, not-running service.
reset_fixture_env
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && ! printf '%s\n' "$out" | grep -Eq '^cloudflared[[:space:]]' \
   && ! grep -q 'cloudflared' "$FX/docker.log"; then
  pass "absent_cloudflared_is_silent: default install neither lists nor updates it"
else
  check_fail "absent_cloudflared_is_silent: rc=$rc log=$(cat "$FX/docker.log") out=$out"
fi

# A v1.2 update changes the pinned ingress source addresses. Every active edge
# must be version-reconciled and recreated with dashboard.
reset_fixture_env
out="$(STUB_ACTIVE_INGRESS='caddy caddy_local cloudflared' run_update --yes)"; rc=$?
dashboard_up="$(grep '^up ' "$FX/docker.log" | grep 'dashboard' | tail -1)"
if [ "$rc" -eq 0 ] \
   && grep '^pull ' "$FX/docker.log" | grep -q 'cloudflared' \
   && printf '%s' "$dashboard_up" | grep -q 'cloudflared' \
   && printf '%s' "$dashboard_up" | grep -q 'caddy' \
   && printf '%s' "$dashboard_up" | grep -q 'caddy_local'; then
  pass "active_ingress_reconciled_with_dashboard: active cloudflared is pulled and recreated in the cohort"
else
  check_fail "active_ingress_reconciled_with_dashboard: rc=$rc line=<<<$dashboard_up>>> log=$(cat "$FX/docker.log") out=$out"
fi

# Declining the application refresh must be authoritative. A third-party-only
# recreate uses --no-deps so an active edge cannot pull dashboard into the
# operation through its Compose dependency.
reset_fixture_env
out="$(STUB_ACTIVE_INGRESS='cloudflared' STUB_UPDATE_INPUT=$'y\nn' run_update)"; rc=$?
third_party_up="$(grep '^up ' "$FX/docker.log" 2>/dev/null || true)"
if [ "$rc" -eq 0 ] \
   && printf '%s' "$third_party_up" | grep -q -- '--no-deps' \
   && printf '%s' "$third_party_up" | grep -q 'cloudflared' \
   && ! printf '%s' "$third_party_up" | grep -q 'dashboard'; then
  pass "third_party_only_recreate_does_not_expand_into_declined_application_services"
else
  check_fail "third-party-only dependency boundary: rc=$rc up=<<<$third_party_up>>> out=<<<$out>>>"
fi

# A pre-v1.2 install may have a custom bridge subnet but none of the exact
# ingress addresses introduced in v1.2. The update must derive, persist, and
# export those addresses before any pull or recreate resolves Compose.
reset_fixture_env
printf 'JARVIS_NET_SUBNET=10.42.8.0/24\n' >> "$FX/.env"
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && grep -qx 'JARVIS_NET_GATEWAY_IP=10.42.8.1' "$FX/.env" \
   && grep -qx 'JARVIS_TELEGRAM_BOT_IP=10.42.8.250' "$FX/.env" \
   && grep -qx 'JARVIS_CADDY_IP=10.42.8.251' "$FX/.env" \
   && grep -qx 'JARVIS_CADDY_LOCAL_IP=10.42.8.252' "$FX/.env" \
   && grep -qx 'JARVIS_DASHBOARD_IP=10.42.8.253' "$FX/.env" \
   && grep -qx 'JARVIS_CLOUDFLARED_IP=10.42.8.254' "$FX/.env" \
   && grep -qx 'network-up DASHBOARD_IP=10.42.8.253' "$FX/docker.log"; then
  pass "legacy_custom_subnet_backfill: exact ingress addresses exist before recreate"
else
  check_fail "legacy_custom_subnet_backfill: rc=$rc env=$(cat "$FX/.env") log=$(cat "$FX/docker.log") out=$out"
fi

# A subnet smaller than /27 cannot hold the pinned ingress cohort. Refuse the
# update before pulling or recreating anything instead of producing a partial
# network migration.
reset_fixture_env
printf 'JARVIS_NET_SUBNET=10.42.8.0/28\n' >> "$FX/.env"
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && ! grep -Eq '^(pull|up) ' "$FX/docker.log" \
   && printf '%s' "$out" | grep -q 'IPv4 /27 or larger'; then
  pass "invalid_ingress_subnet_aborts_before_mutation"
else
  check_fail "invalid_ingress_subnet_aborts_before_mutation: rc=$rc log=$(cat "$FX/docker.log") out=$out"
fi

# An update is checkout-based: `git pull` delivers tracked files, but the
# generated secrets/*.txt are not tracked. A release that begins mounting a new
# Docker secret therefore reaches an older install with that file absent, and
# Compose aborts with "secret not found" partway through recreating services.
# ORDERING is the contract under test, not the mere presence of the call: a
# check that only grepped for init-secrets would also pass if the call sat after
# the recreate, which is precisely the failure being prevented.
reset_fixture_env
out="$(run_update --yes)"; rc=$?
secrets_at="$(grep -n 'init-secrets' "$FX/docker.log" | head -1 | cut -d: -f1)"
stage_at="$(grep -nE '^(pull|build|up|network-up) ' "$FX/docker.log" | head -1 | cut -d: -f1)"
if [ "$rc" -eq 0 ] && [ -n "$secrets_at" ] && [ -n "$stage_at" ] \
   && [ "$secrets_at" -lt "$stage_at" ]; then
  pass "required_secrets_are_created_before_the_first_image_is_staged"
else
  check_fail "secrets phase did not precede staging: rc=$rc secrets_at=${secrets_at:-none} stage_at=${stage_at:-none} log=$(cat "$FX/docker.log") out=$out"
fi

# A secret that cannot be written must stop the update while the running cohort
# is still untouched -- nothing pulled, built or recreated -- and must name the
# script the operator has to run by hand.
reset_fixture_env
out="$(STUB_FAIL_SECRETS=1 run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && ! grep -Eq '^(pull|build|up|network-up) ' "$FX/docker.log" \
   && printf '%s' "$out" | grep -q 'init-secrets.sh'; then
  pass "unwritable_secrets_abort_the_update_before_any_image_is_staged"
else
  check_fail "unwritable secrets did not abort before staging: rc=$rc log=$(cat "$FX/docker.log") out=$out"
fi

# A staging failure must not claim that the old deployment advanced.
reset_fixture_env
out="$(STUB_FAIL_PULL_MATCH=dashboard run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.3' "$FX/.env"; then
  pass "staging_failure_preserves_persisted_identities"
else
  check_fail "staging_failure_preserves_persisted_pin: rc=$rc env=$(cat "$FX/.env") out=$out"
fi

# A pre-v1.2.2 installation has no dedicated image tag. Recovery must use its
# semantic version as the legacy image identity without mutating the failed run.
reset_fixture_env
grep -v '^JARVIS_IMAGE_TAG=' "$FX/.env" > "$FX/.env.legacy"
mv "$FX/.env.legacy" "$FX/.env"
out="$(STUB_FAIL_PULL_MATCH=dashboard run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && printf '%s' "$out" | grep -q 'JARVIS_IMAGE_TAG=1.1.3 docker compose' \
   && ! grep -q '^JARVIS_IMAGE_TAG=' "$FX/.env" \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$FX/.env"; then
  pass "legacy application version supplies recovery image identity"
else
  check_fail "legacy image-tag fallback: rc=$rc env=$(cat "$FX/.env") out=$out"
fi

# An exact release tag is more specific than pyproject's stable project version.
reset_fixture_env
out="$(STUB_EXACT_TAG=v1.2.0-rc.1 run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && grep -Eq '^pull .*dashboard.*JARVIS_VERSION=1\.2\.0-rc\.1 JARVIS_IMAGE_TAG=1\.2\.0-rc\.1$' "$FX/docker.log" \
   && grep -qx 'JARVIS_VERSION=1.2.0-rc.1' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.2.0-rc.1' "$FX/.env"; then
  pass "exact_rc_tag_wins: semantic version and image tag use 1.2.0-rc.1"
else
  check_fail "exact_rc_tag_wins: rc=$rc env=$(cat "$FX/.env") log=$(cat "$FX/docker.log") out=$out"
fi

# Local builds consume the same resolved tag and commit it only after bring-up.
reset_fixture_env
out="$(run_update --build-local --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && grep -Eq '^build .*dashboard.*JARVIS_VERSION=1\.2\.0 JARVIS_IMAGE_TAG=1\.2\.0$' "$FX/docker.log" \
   && grep -Eq '^up .*dashboard.*JARVIS_VERSION=1\.2\.0 JARVIS_IMAGE_TAG=1\.2\.0$' "$FX/docker.log" \
   && grep -qx 'JARVIS_VERSION=1.2.0' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.2.0' "$FX/.env"; then
  pass "build_local_uses_checkout_version for both identities"
else
  check_fail "build_local_uses_checkout_version: rc=$rc env=$(cat "$FX/.env") log=$(cat "$FX/docker.log") out=$out"
fi

# Invalid or absent checkout metadata must fail before Compose is invoked and
# must leave the deployment pin untouched.
reset_fixture_env
sed 's/version = "1.2.0"/version = "not valid"/' "$FX/pyproject.toml" > "$FX/pyproject.invalid"
mv "$FX/pyproject.invalid" "$FX/pyproject.toml"
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && [ ! -s "$FX/docker-calls.log" ] \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.3' "$FX/.env"; then
  pass "invalid_checkout_version_fails_before_compose: no Docker call and old pin remains"
else
  check_fail "invalid_checkout_version_fails_before_compose: rc=$rc calls=$(cat "$FX/docker-calls.log") env=$(cat "$FX/.env") out=$out"
fi
rm -f "$FX/pyproject.toml"
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && [ ! -s "$FX/docker-calls.log" ] \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.3' "$FX/.env"; then
  pass "missing_checkout_version_fails_before_compose: no Docker call and old pin remains"
else
  check_fail "missing_checkout_version_fails_before_compose: rc=$rc calls=$(cat "$FX/docker-calls.log") env=$(cat "$FX/.env") out=$out"
fi
cat > "$FX/pyproject.toml" <<'PYPROJECT'
[project]
name = "jarvis-rd-assistant"
version = "1.2.0"
PYPROJECT

# --- update_yes_runs_promptless ----------------------------------------------
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] && grep -q '^pull ' "$FX/docker.log"; then
  pass "update_yes_runs_promptless: --yes proceeds past prompts on closed stdin"
else
  check_fail "update_yes_runs_promptless: rc=$rc, log=$(cat "$FX/docker.log")"
fi
# Contrast: without --yes, a closed stdin answers no -> nothing pulled.
noyes_log="$FX/docker.log"; out="$(run_update)"; rc=$?
if [ "$rc" -eq 0 ] && ! grep -q '^pull ' "$noyes_log"; then
  pass "no --yes on closed stdin: nothing is pulled (prompts default to no)"
else
  check_fail "no-yes contrast: rc=$rc, log=$(cat "$noyes_log")"
fi

# --- pulls_complete_before_any_recreate --------------------------------------
out="$(run_update --yes)"
last_pull="$(grep -n '^pull ' "$FX/docker.log" | tail -1 | cut -d: -f1)"
first_up="$(grep -n '^up '   "$FX/docker.log" | head -1 | cut -d: -f1)"
if [ -n "$last_pull" ] && [ -n "$first_up" ] && [ "$last_pull" -lt "$first_up" ]; then
  pass "pulls_complete_before_any_recreate: last pull precedes first up"
else
  check_fail "pulls_complete_before_any_recreate: last_pull=$last_pull first_up=$first_up log=$(cat "$FX/docker.log")"
fi

# --- no_healthcheck_reported_not_silent --------------------------------------
out="$(STUB_HEALTH="" run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && printf '%s' "$out" | grep -q 'running (no healthcheck)' \
   && printf '%s' "$out" | grep -q 'not health-verified' \
   && ! printf '%s' "$out" | grep -q 'dashboard: healthy'; then
  pass "no_healthcheck_reported_not_silent: reported as running-not-verified, never healthy"
else
  check_fail "no_healthcheck_reported_not_silent: rc=$rc out=$out"
fi

# An absent healthcheck is successful only while the container is running.
reset_fixture_env
out="$(STUB_HEALTH="" STUB_RUN_STATE=exited run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && printf '%s' "$out" | grep -q 'not running (state: exited)' \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$FX/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.3' "$FX/.env"; then
  pass "no_healthcheck_exited_fails_without_pin_commit"
else
  check_fail "no-healthcheck terminal state: rc=$rc env=$(cat "$FX/.env") out=<<<$out>>>"
fi

# --- update_success_installs_shim --------------------------------------------
rm -rf "$FX/home"
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && [ -x "$FX/home/.local/bin/jarvis-research" ] \
   && grep -qxF "$FX" "$FX/home/.config/jarvis-research/installs"; then
  pass "update_success_installs_shim: launcher installed and path registered on success"
else
  check_fail "update_success_installs_shim: rc=$rc shim=$(ls -l "$FX/home/.local/bin" 2>/dev/null) installs=$(cat "$FX/home/.config/jarvis-research/installs" 2>/dev/null)"
fi
# Failure runs must not install it.
rm -rf "$FX/home"
out="$(STUB_FAIL_PULL=1 run_update --yes)" || true
if [ ! -e "$FX/home/.local/bin/jarvis-research" ]; then
  pass "update_failure_skips_shim_install: failed update leaves no launcher behind"
else
  check_fail "update_failure_skips_shim_install: launcher appeared on a failed update"
fi

# --- die_paths_print_recovery (pull failure) ---------------------------------
out="$(STUB_FAIL_PULL=1 run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && printf '%s' "$out" | grep -q 'Recovery is limited to the failed image services' \
   && printf '%s' "$out" | grep -q 'Third-party services' \
   && ! grep -q '^up ' "$FX/docker.log"; then
  pass "die_paths_print_recovery: pull failure prints bounded recovery, exits 1, recreates nothing"
else
  check_fail "die_paths_print_recovery: rc=$rc out=$out log=$(cat "$FX/docker.log")"
fi

# =============================================================================
# Pure release helpers (source setup_lib.sh, stub git/docker on PATH).
# =============================================================================
mkstub() { mkdir -p "$1"; cat > "$1/$2"; chmod +x "$1/$2"; }

# --- latest_stable_tag: excludes pre-releases, sorts versionally -------------
GITSTUB="$(mktemp -d)"
mkstub "$GITSTUB" git <<'GIT'
#!/usr/bin/env bash
if [ "${1:-}" = "ls-remote" ]; then
  cat <<'TAGS'
sha refs/tags/v0.9.9
sha refs/tags/v1.9.0
sha refs/tags/v1.10.0
sha refs/tags/v1.10.0-rc2
sha refs/tags/v2.0.0-rc1
TAGS
  exit 0
fi
exit 0
GIT
mkstub "$GITSTUB" sort <<'SORT'
#!/usr/bin/env bash
printf 'GNU sort -V must not be required\n' >&2
exit 99
SORT
got="$(PATH="$GITSTUB:$PATH" latest_stable_tag origin)"
if [ "$got" = "v1.10.0" ]; then
  pass "latest_stable_tag_sorts_versionally: v1.10.0 > v1.9.0"
else
  check_fail "latest_stable_tag_sorts_versionally: got '$got' (want v1.10.0)"
fi
case "$got" in
  *-rc*) check_fail "latest_stable_tag_excludes_rc: returned a pre-release '$got'" ;;
  *)     pass "latest_stable_tag_excludes_rc: no -rc tag selected" ;;
esac
rm -rf "$GITSTUB"

# --- install_cli_shim: idempotent + prepend-dedup ----------------------------
CLIROOT="$(mktemp -d)"
export JARVIS_CLI_BIN_DIR="$CLIROOT/bin"
export JARVIS_CLI_CONFIG_DIR="$CLIROOT/cfg"
repoA="$CLIROOT/repoA"; repoB="$CLIROOT/repoB"
out1="$(install_cli_shim "$repoA")"
out2="$(install_cli_shim "$repoA")"
shim="$JARVIS_CLI_BIN_DIR/jarvis-research"
installs="$JARVIS_CLI_CONFIG_DIR/installs"
if [ -n "$out1" ] && [ -z "$out2" ]; then
  pass "install_cli_shim_idempotent: prints on first install, silent on re-run"
else
  check_fail "install_cli_shim_idempotent: out1='$out1' out2='$out2'"
fi
if [ -x "$shim" ] && grep -q 'scripts/jarvis-research.sh' "$shim" && grep -q -- '--repo' "$shim"; then
  pass "install_cli_shim: launcher is executable and execs the repo CLI with --repo"
else
  check_fail "install_cli_shim: launcher missing/wrong"
fi
if [ "$(head -n 1 "$installs")" = "$repoA" ]; then
  pass "install_cli_shim: repo registered at the top of the registry"
else
  check_fail "install_cli_shim: registry top is '$(head -n 1 "$installs")'"
fi
install_cli_shim "$repoB" >/dev/null
if [ "$(head -n 1 "$installs")" = "$repoB" ] && grep -qxF "$repoA" "$installs" \
   && [ "$(grep -cxF "$repoA" "$installs")" = 1 ]; then
  pass "install_cli_shim: a second repo prepends and de-dups the registry"
else
  check_fail "install_cli_shim: dedup/prepend wrong ($(tr '\n' ',' < "$installs"))"
fi
unset JARVIS_CLI_BIN_DIR JARVIS_CLI_CONFIG_DIR
rm -rf "$CLIROOT"

# --- verify_release_manifests ------------------------------------------------
# git stub emits the target ref's versions.env; docker stub logs every inspected
# ref and passes/fails per MANIFEST_MISS.
VSTUB="$(mktemp -d)"
mkstub "$VSTUB" git <<'GIT'
#!/usr/bin/env bash
if [ "${1:-}" = "show" ]; then
  cat <<'VE'
POSTGRES_IMAGE=postgres:16.8@sha256:aaa
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
LITELLM_IMAGE=litellm@sha256:bbb
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2025.1.0
CADDY_IMAGE=caddy:2.9-alpine
VE
  exit 0
fi
exit 0
GIT
mkstub "$VSTUB" docker <<'DOCKER'
#!/usr/bin/env bash
# docker manifest inspect <ref>
if [ "${1:-}" = "manifest" ] && [ "${2:-}" = "inspect" ]; then
  ref="$3"
  [ -n "${MANIFEST_LOG:-}" ] && printf '%s\n' "$ref" >> "$MANIFEST_LOG"
  if [ -n "${MANIFEST_MISS:-}" ] && printf '%s' "$ref" | grep -q "$MANIFEST_MISS"; then
    exit 1
  fi
  exit 0
fi
exit 0
DOCKER
export TORCH_VARIANT_SUFFIX=""

# all present -> rc 0
out="$( PATH="$VSTUB:$PATH" MANIFEST_LOG="" verify_release_manifests v1.1.3 )"; rc=$?
if [ "$rc" -eq 0 ] \
   && printf '%s' "$out" | grep -q 'PRESENT ghcr.io/limitcycle-oss/jarvis-dashboard:1.1.3' \
   && ! printf '%s' "$out" | grep -q 'MISSING'; then
  pass "manifest_verify_all_present_rc0: every image present -> rc 0"
else
  check_fail "manifest_verify_all_present_rc0: rc=$rc out=$out"
fi

# one missing -> rc 1
out="$( PATH="$VSTUB:$PATH" MANIFEST_MISS='jarvis-dashboard:1.1.3' verify_release_manifests v1.1.3 )"; rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'MISSING ghcr.io/limitcycle-oss/jarvis-dashboard:1.1.3'; then
  pass "manifest_verify_missing_rc1: a missing image -> rc 1 + MISSING line"
else
  check_fail "manifest_verify_missing_rc1: rc=$rc out=$out"
fi

# app image refs never carry the tag's v-prefix
MLOG="$(mktemp)"
out="$( PATH="$VSTUB:$PATH" MANIFEST_LOG="$MLOG" verify_release_manifests v1.1.3 )"
if grep -q ':1\.1\.3' "$MLOG" && ! grep -q ':v1\.1\.3' "$MLOG"; then
  pass "manifest_names_never_carry_v_prefix: image tags are :1.1.3, never :v1.1.3"
else
  check_fail "manifest_names_never_carry_v_prefix: log=$(tr '\n' ' ' < "$MLOG")"
fi
rm -f "$MLOG"

# observability active: langfuse (local-build) skipped, gate still passes
MLOG="$(mktemp)"
out="$( PATH="$VSTUB:$PATH" MANIFEST_LOG="$MLOG" verify_release_manifests v1.1.3 observability )"; rc=$?
if [ "$rc" -eq 0 ] \
   && printf '%s' "$out" | grep -q 'SKIPPED jarvis/langfuse-hardened:1.1.3' \
   && ! grep -q 'langfuse-hardened' "$MLOG"; then
  pass "manifest_gate_skips_local_build_images: langfuse SKIPPED, never inspected, gate passes"
else
  check_fail "manifest_gate_skips_local_build_images: rc=$rc out=$out log=$(tr '\n' ' ' < "$MLOG")"
fi
rm -f "$MLOG"
unset TORCH_VARIANT_SUFFIX
rm -rf "$VSTUB"

# =============================================================================
if [ "$fail" -ne 0 ]; then
  printf '\nupdate.sh coverage: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nupdate.sh coverage: all %s checks passed\n' "$pass_n"
