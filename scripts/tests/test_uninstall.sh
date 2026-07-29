#!/usr/bin/env bash
# test_uninstall.sh — behavioral tests for scripts/uninstall.sh, the tiered,
# contained teardown of a managed JARVIS install. No docker daemon, network, or
# real git repo is needed: docker (and `docker compose`) are stubbed on a private
# PATH that LOGS every invocation (the pattern from test_update_coverage.sh /
# test_jarvis_research_cli.sh), and the script runs against throwaway fixture
# clones under an isolated HOME.
#
# The containment matrix is the specification: every refusal must exit non-zero
# and leave the docker stub log free of a mutating verb (down/rmi), the on-disk
# secrets/ untouched, and the destructive typed gates (data project name, purge
# phrase, per-image confirmations) must stay mandatory even under --yes/--all.
#
# Run: bash scripts/tests/test_uninstall.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UNINSTALL="${REPO_ROOT}/scripts/uninstall.sh"
LIB="${REPO_ROOT}/scripts/setup_lib.sh"
LIFECYCLE_HELPER="${REPO_ROOT}/scripts/backup-lifecycle.sh"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
check_fail() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }
has()  { printf '%s' "$1" | grep -q -- "$2"; }
want() { if has "$1" "$2"; then pass "$3"; else check_fail "$3 :: missing /$2/ in <<<$1>>>"; fi; }
lack() { if has "$1" "$2"; then check_fail "$3 :: unexpected /$2/ in <<<$1>>>"; else pass "$3"; fi; }

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT" 2>/dev/null || true' EXIT
STUB="$ROOT/stub"
mkdir -p "$STUB"
HOMEDIR="$ROOT/home"          # isolated HOME (its parent, $ROOT, is an ancestor)
mkdir -p "$HOMEDIR"

# =============================================================================
# docker stub: logs every mutating call to $DOCKER_LOG. `info` reports the daemon
# reachable unless STUB_NO_DAEMON=1; `compose ps -q` reports the stack down.
# =============================================================================
cat > "$STUB/docker" <<'DOCKER'
#!/usr/bin/env bash
log() { [ -n "${DOCKER_LOG:-}" ] && printf 'docker %s\n' "$*" >> "$DOCKER_LOG"; }
case "${1:-}" in
  info) [ "${STUB_NO_DAEMON:-0}" = 1 ] && exit 1; exit 0 ;;
  run)
    raw_args=("$@")
    for ((i=0; i<${#raw_args[@]}; i++)); do
      if [ "${raw_args[$i]}" = /tmp/backup-lifecycle.sh ]; then
        helper_args=("${raw_args[@]:$((i + 1))}")
        cmd="${helper_args[0]:-}"
        lifecycle="$STUB_BACKUP_DIR/.lifecycle"
        state="$lifecycle/operation.state"
        reservation="$lifecycle/host.reservation"
        mkdir -p "$lifecycle"
        log "lifecycle ${helper_args[*]}"
        case "$cmd" in
          current-host) [ ! -s "$state" ] || cat "$state" ;;
          reserve-host)
            expected="${helper_args[1]}:${helper_args[2]}"
            if [ -s "$state" ] && [ "$(cat "$state")" != "$expected" ]; then exit 1; fi
            if [ -s "$reservation" ] && [ "$(cat "$reservation")" != "$expected" ]; then exit 1; fi
            printf '%s\n' "$expected" > "$reservation"
            printf 'launch\n' ;;
          hold-host)
            expected="${helper_args[1]}:${helper_args[2]}"
            [ -s "$reservation" ] && [ "$(cat "$reservation")" = "$expected" ] || exit 1
            printf '%s\n' "$expected" > "$state"
            printf '0123456789abcdef0123456789abcdef\n' ;;
          wait-host|host-status)
            expected="${helper_args[1]}:${helper_args[2]}"
            [ -s "$state" ] && [ "$(cat "$state")" = "$expected" ] ;;
          release-host)
            expected="${helper_args[1]}:${helper_args[2]}"
            [ -s "$state" ] && [ "$(cat "$state")" = "$expected" ] || exit 1
            [ "${helper_args[3]}" != clear ] || rm -f "$state" "$reservation" ;;
          host-release-complete)
            expected="${helper_args[1]}:${helper_args[2]}"
            if [ "${helper_args[3]}" = clear ]; then
              [ ! -e "$state" ] && [ ! -e "$reservation" ]
            else
              [ -s "$state" ] && [ "$(cat "$state")" = "$expected" ] \
                && [ -s "$reservation" ] && [ "$(cat "$reservation")" = "$expected" ]
            fi ;;
          clear-retained-host)
            expected="${helper_args[1]}:${helper_args[2]}"
            [ -s "$state" ] && [ "$(cat "$state")" = "$expected" ] || exit 1
            rm -f "$state" "$reservation" ;;
          *) exit 1 ;;
        esac
        exit $?
      fi
    done
    exit 0 ;;
  ps)
    [ "${STUB_FAIL_CONTAINER_LIST:-0}" = 1 ] && exit 1
    [ "${STUB_NO_PROJECT_CONTAINERS:-0}" = 1 ] \
      || printf '%s\n' "${STUB_COMPOSE_CONTAINER_ID:-cid-managed}"
    exit 0 ;;
  rmi)
    log "$*"
    # STUB_FAIL_RMI=<ref> makes rmi of exactly that ref fail (image absent).
    if [ -n "${STUB_FAIL_RMI:-}" ]; then
      for a in "$@"; do [ "$a" = "${STUB_FAIL_RMI}" ] && exit 1; done
    fi
    exit 0 ;;
  compose)
    shift
    raw="$*"
    while [ $# -gt 0 ]; do
      case "$1" in
        --project-directory|--env-file|-p|--project-name|-f|--file) shift 2 ;;
        --ansi|--progress|--parallel) shift 2 ;;
        *) break ;;
      esac
    done
    case "${1:-}" in
      config)
        printf '{"volumes":{"postgres_backups":{"name":"%s_postgres_backups"}}}\n' \
          "$STUB_EXPECTED_PROJECT"
        exit 0 ;;
      ps)
        case " $* " in
          *" -aq "*|*" -qa "*) printf '%s\n' "${STUB_COMPOSE_CONTAINER_ID:-cid-managed}" ;;
          *) [ "${STUB_STACK_UP:-0}" = 1 ] && printf '%s\n' "${STUB_COMPOSE_CONTAINER_ID:-cid-managed}" ;;
        esac
        exit 0 ;;
      down) log "compose $raw"; exit 0 ;;
      run)
        log "compose $raw"
        if [ "${STUB_BACKUP_BUSY:-0}" = 1 ]; then
          printf '[backup] another backup is already running; skipping\n'
          exit 0
        fi
        [ "${STUB_FAIL_BACKUP:-0}" != 1 ]
        exit $? ;;
      stop)
        log "compose $raw"
        if [ -n "${STUB_FAIL_REQUESTER_STOP_ONCE_FILE:-}" ] \
           && [ ! -e "$STUB_FAIL_REQUESTER_STOP_ONCE_FILE" ]; then
          : > "$STUB_FAIL_REQUESTER_STOP_ONCE_FILE"
          exit 1
        fi
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
  inspect)
    [ "${STUB_INSPECT_FAIL:-0}" = 1 ] && exit 1
    target="${*: -1}"
    fmt=""
    while [ $# -gt 0 ]; do
      case "$1" in --format|-f) fmt="$2"; shift 2 ;; *) shift ;; esac
    done
    case "$fmt" in
      *com.docker.compose.project.working_dir*)
        inspect_count=0
        [ -f "${STUB_INSPECT_COUNT_FILE:-}" ] && inspect_count="$(cat "$STUB_INSPECT_COUNT_FILE")"
        inspect_count=$((inspect_count + 1))
        [ -n "${STUB_INSPECT_COUNT_FILE:-}" ] && printf '%s\n' "$inspect_count" > "$STUB_INSPECT_COUNT_FILE"
        label_project="${STUB_LABEL_PROJECT:-${STUB_EXPECTED_PROJECT:-}}"
        label_working_dir="${STUB_LABEL_WORKING_DIR:-${STUB_EXPECTED_REPO:-}}"
        label_config_files="${STUB_LABEL_CONFIG_FILES:-${STUB_EXPECTED_CONFIG:-}}"
        if [ "${STUB_REVERIFY_FOREIGN:-0}" = 1 ] && [ "$inspect_count" -ge 2 ]; then
          label_project=foreign-project
        fi
        if [ "${STUB_SECOND_CONTAINER_FOREIGN:-0}" = 1 ] && [ "$target" = cid-foreign ]; then
          label_project=foreign-project
        fi
        case "${STUB_MISSING_LABEL:-}" in
          project) label_project="" ;;
          working-dir) label_working_dir="" ;;
          config-files) label_config_files="" ;;
        esac
        printf '%s|%s|%s\n' \
          "$label_project" \
          "$label_working_dir" \
          "$label_config_files" ;;
      *com.docker.compose.project*com.docker.compose.volume*)
        printf '%s|%s\n' \
          "${STUB_VOLUME_PROJECT:-${STUB_EXPECTED_PROJECT:-}}" \
          "${STUB_VOLUME_LOGICAL:-}" ;;
      *) : ;;
    esac
    exit 0 ;;
  network)
    case "${2:-}" in
      ls)
        [ "${STUB_FAIL_NETWORK_LIST:-0}" = 1 ] && exit 1
        [ "${STUB_PROJECT_NETWORK:-0}" = 1 ] && printf 'network-managed\n'
        exit 0 ;;
    esac ;;
  volume)
    case "${2:-}" in
      ls) printf '%s' "${STUB_VOLUME_NAMES:-}"; [ -z "${STUB_VOLUME_NAMES:-}" ] || printf '\n'; exit 0 ;;
      inspect)
        target="${*: -1}"
        if [ "$target" = "${STUB_EXPECTED_PROJECT:-}_postgres_backups" ]; then
          printf '%s|postgres_backups\n' "${STUB_LIFECYCLE_VOLUME_PROJECT:-${STUB_EXPECTED_PROJECT:-}}"
        else
          printf '%s|%s\n' \
            "${STUB_VOLUME_PROJECT:-${STUB_EXPECTED_PROJECT:-}}" \
            "${STUB_VOLUME_LOGICAL:-}"
        fi
        exit 0 ;;
      create) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
DOCKER
chmod +x "$STUB/docker"

# Stock macOS does not provide GNU realpath (and its realpath has no -m). The
# uninstall must use its Python prerequisite for all containment decisions.
cat > "$STUB/realpath" <<'REALPATH'
#!/usr/bin/env bash
exit 64
REALPATH
chmod +x "$STUB/realpath"

# =============================================================================
# Fixture clone builder: a valid JARVIS install with the real volumes/networks
# blocks, image pins, runtime files, and a backup encryption key.
# =============================================================================
make_clone() {
  local dir="$1"
  mkdir -p "$dir/scripts" "$dir/secrets" "$dir/shared"
  ln -sf "$LIB" "$dir/scripts/setup_lib.sh"
  cp "$LIFECYCLE_HELPER" "$dir/scripts/backup-lifecycle.sh"
  cat > "$dir/versions.env" <<'VE'
POSTGRES_IMAGE=postgres:16.8
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
CADDY_IMAGE=caddy:2.9-alpine
VE
  cat > "$dir/pyproject.toml" <<'PYPROJECT'
[project]
name = "jarvis-rd-assistant"
version = "1.1.3"
PYPROJECT
  cat > "$dir/docker-compose.yml" <<'YML'
services:
  dashboard:
    image: x
volumes:
  postgres_data:
  ollama_data:
  qdrant_data:
  postgres_backups:
  backup_trigger:
  restore_staging:  # staged snapshots
  restore_inbox:  # cross-host DR drop zone
  hf_cache:
  caddy_data:
  caddy_config:
  langfuse_postgres_data:
  vector_data:
networks:
  jarvis:
    driver: bridge
YML
  printf 'JARVIS_VERSION=1.1.3\nTORCH_VARIANT=cuda\nTORCH_VARIANT_SUFFIX=-cuda\n' > "$dir/.env"
  printf 'SUPERSECRETKEY\n' > "$dir/secrets/backup_encrypt_key.txt"
  printf 'shared-data\n' > "$dir/shared/marker.txt"
}

# The application image refs uninstall.sh must derive from this fixture (.env
# pins JARVIS_VERSION=1.1.3, TORCH_VARIANT_SUFFIX=-cuda; no telegram token).
NS="ghcr.io/limitcycle-oss/jarvis-"
APP_REFS=(
  "${NS}paper-ingestion:1.1.3-cuda"
  "${NS}learning-engine:1.1.3"
  "${NS}dashboard:1.1.3"
  "${NS}restore-uploader:1.1.3"
)
# new_env — a fresh fixture clone, state dir, bin dir, and stub log.
new_env() {
  CLONE="$ROOT/clone.$RANDOM.$RANDOM"
  CFG="$ROOT/cfg.$RANDOM.$RANDOM"
  BIN="$ROOT/bin.$RANDOM.$RANDOM"
  mkdir -p "$CFG" "$BIN"
  make_clone "$CLONE"
  DOCKER_LOG="$ROOT/dlog.$RANDOM"
  : > "$DOCKER_LOG"
  INSPECT_COUNT_FILE="$ROOT/inspect-count.$RANDOM"
  printf '0\n' > "$INSPECT_COUNT_FILE"
  BK="$ROOT/backups.$RANDOM.$RANDOM"
  mkdir -p "$BK/.lifecycle"
  REQUESTER_STOP_FAIL_FILE="$ROOT/requester-stop-fail.$RANDOM"
}

# run_un [--stdin <data>] <args...> — invoke uninstall.sh with the stub PATH and
# the fixture env overrides. Default feeds an empty stdin (closed).
run_un() {
  local stdin_data=""
  if [ "${1:-}" = "--stdin" ]; then stdin_data="$2"; shift 2; fi
  local expected_project expected_config
  expected_project="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  expected_config="$CLONE/docker-compose.yml"
  if [ -f "$CLONE/docker-compose.override.yml" ] && ! grep -q '^COMPOSE_FILE=' "$CLONE/.env"; then
    expected_config="$expected_config,$CLONE/docker-compose.override.yml"
  fi
  env "PATH=$STUB:$PATH" "DOCKER_LOG=$DOCKER_LOG" "STUB_NO_DAEMON=${STUB_NO_DAEMON:-0}" \
    "STUB_FAIL_RMI=${STUB_FAIL_RMI:-}" \
    "STUB_COMPOSE_CONTAINER_ID=${STUB_COMPOSE_CONTAINER_ID:-cid-managed}" \
    "STUB_NO_PROJECT_CONTAINERS=${STUB_NO_PROJECT_CONTAINERS:-0}" \
    "STUB_FAIL_CONTAINER_LIST=${STUB_FAIL_CONTAINER_LIST:-0}" \
    "STUB_STACK_UP=${STUB_STACK_UP:-0}" \
    "STUB_FAIL_BACKUP=${STUB_FAIL_BACKUP:-0}" \
    "STUB_BACKUP_BUSY=${STUB_BACKUP_BUSY:-0}" \
    "STUB_EXPECTED_PROJECT=$expected_project" "STUB_EXPECTED_REPO=$CLONE" \
    "STUB_EXPECTED_CONFIG=$expected_config" \
    "STUB_LABEL_PROJECT=${STUB_LABEL_PROJECT:-}" \
    "STUB_LABEL_WORKING_DIR=${STUB_LABEL_WORKING_DIR:-}" \
    "STUB_LABEL_CONFIG_FILES=${STUB_LABEL_CONFIG_FILES:-}" \
    "STUB_MISSING_LABEL=${STUB_MISSING_LABEL:-}" \
    "STUB_INSPECT_FAIL=${STUB_INSPECT_FAIL:-0}" \
    "STUB_INSPECT_COUNT_FILE=$INSPECT_COUNT_FILE" \
    "STUB_REVERIFY_FOREIGN=${STUB_REVERIFY_FOREIGN:-0}" \
    "STUB_SECOND_CONTAINER_FOREIGN=${STUB_SECOND_CONTAINER_FOREIGN:-0}" \
    "STUB_PROJECT_NETWORK=${STUB_PROJECT_NETWORK:-0}" \
    "STUB_FAIL_NETWORK_LIST=${STUB_FAIL_NETWORK_LIST:-0}" \
    "STUB_VOLUME_NAMES=${STUB_VOLUME_NAMES:-}" \
    "STUB_VOLUME_PROJECT=${STUB_VOLUME_PROJECT:-}" \
    "STUB_VOLUME_LOGICAL=${STUB_VOLUME_LOGICAL:-}" \
    "STUB_LIFECYCLE_VOLUME_PROJECT=${STUB_LIFECYCLE_VOLUME_PROJECT:-}" \
    "STUB_BACKUP_DIR=$BK" \
    "STUB_FAIL_REQUESTER_STOP_ONCE_FILE=${STUB_FAIL_REQUESTER_STOP_ONCE_FILE:-}" \
    "HOME=$HOMEDIR" "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$BIN" \
    bash "$UNINSTALL" "$@" <<<"$stdin_data" 2>&1
}

log_has()  { grep -q -- "$1" "$DOCKER_LOG" 2>/dev/null; }
mutation_log_empty() {  # $1 = description
  if grep -qE 'compose .* down|rmi ' "$DOCKER_LOG" 2>/dev/null; then
    check_fail "$1 :: mutation in docker log: $(tr '\n' ';' < "$DOCKER_LOG")"
  else
    pass "$1"
  fi
}

# =============================================================================
# 1. Mechanical: banned bulk-prune primitives never appear in the script text.
# =============================================================================
if grep -qE '(system|image|volume|network|builder) prune' "$UNINSTALL"; then
  check_fail "banned_primitives_absent: a bulk 'prune' primitive is present in the script"
else
  pass "banned_primitives_absent: no system/image/volume/network/builder prune in the script"
fi
if grep -Eq 'declare[[:space:]]+-A|\bmapfile\b|\brealpath\b' "$UNINSTALL"; then
  check_fail "stock_macos_bash_contract: Bash-4/GNU-only uninstall primitive remains"
else
  pass "stock_macos_bash_contract: no associative array, mapfile, or realpath dependency"
fi

# =============================================================================
# 2. Containment: dangerous / unmanaged targets refuse before any mutation.
# =============================================================================
new_env
out="$(run_un --repo / --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ]; then pass "refuses_root: filesystem root -> nonzero exit"; else check_fail "refuses_root: rc=$rc out=$out"; fi
mutation_log_empty "refuses_root: empty mutation log"

new_env
out="$(run_un --repo "$HOMEDIR" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ]; then pass "refuses_home: \$HOME -> nonzero exit"; else check_fail "refuses_home: rc=$rc out=$out"; fi
mutation_log_empty "refuses_home: empty mutation log"

new_env
out="$(run_un --repo "$ROOT" --tier 4 --yes)"; rc=$?   # $ROOT is an ancestor of HOME
if [ "$rc" -ne 0 ]; then pass "refuses_ancestor_of_home: ancestor path -> nonzero exit"; else check_fail "refuses_ancestor_of_home: rc=$rc out=$out"; fi
mutation_log_empty "refuses_ancestor_of_home: empty mutation log"

# symlink is canonicalized BEFORE the identity check; the refusal names the
# canonical (resolved) path, not the link.
new_env
mkdir -p "$ROOT/nonproject"
ln -sf "$ROOT/nonproject" "$ROOT/aliaslink"
out="$(run_un --repo "$ROOT/aliaslink" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && has "$out" "$ROOT/nonproject"; then
  pass "resolves_symlink_before_identity_check: refusal names the canonical path"
else
  check_fail "resolves_symlink_before_identity_check: rc=$rc out=<<<$out>>>"
fi
mutation_log_empty "resolves_symlink_before_identity_check: empty mutation log"

new_env
mkdir -p "$ROOT/plaindir"
out="$(run_un --repo "$ROOT/plaindir" --tier 2 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && has "$out" 'not a JARVIS install'; then
  pass "refuses_unmanaged_dir: a non-JARVIS directory -> refusal"
else
  check_fail "refuses_unmanaged_dir: rc=$rc out=<<<$out>>>"
fi
mutation_log_empty "refuses_unmanaged_dir: empty mutation log"

# Ambient Compose selectors are caller-controlled and must never redirect either
# discovery or teardown away from the canonical install selected by --repo.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
foreign="$ROOT/foreign-compose.yml"
printf 'services:\n  trap:\n    image: foreign\n' > "$foreign"
out="$(COMPOSE_FILE="$foreign" COMPOSE_PROJECT_NAME=foreign-project \
  COMPOSE_PROFILES=foreign-profile COMPOSE_PATH_SEPARATOR=';' \
  COMPOSE_ENV_FILES="$ROOT/foreign.env" COMPOSE_DISABLE_ENV_FILE=1 \
  run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
compose_line="$(grep 'compose .* down' "$DOCKER_LOG" 2>/dev/null || true)"
if [ "$rc" -eq 0 ] \
   && has "$compose_line" "--project-directory $CLONE" \
   && has "$compose_line" "--env-file $CLONE/.env" \
   && has "$compose_line" "-p $proj" \
   && has "$compose_line" "-f $CLONE/docker-compose.yml" \
   && ! has "$compose_line" 'foreign-project\|foreign-compose\|foreign-profile'; then
  pass "ambient_compose_selectors_cannot_redirect_teardown: canonical explicit compose target used"
else
  check_fail "ambient_compose_selectors_cannot_redirect_teardown: rc=$rc line=<<<$compose_line>>> out=<<<$out>>>"
fi

# A lifecycle operation already running for this exact installation blocks
# teardown before Docker receives any mutating command.
new_env
lock_id="$(printf '%s' "$(realpath "$CLONE")" | sha256sum | cut -d' ' -f1)"
mkdir -p "$CFG/locks"
exec 7>"$CFG/locks/${lock_id}.lock"
flock -n 7
out="$(run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
flock -u 7; exec 7>&-
if [ "$rc" -eq 1 ] && ! log_has 'compose .*down'; then
  pass "uninstall_refuses_concurrent_lifecycle_operation: no teardown mutation"
else
  check_fail "uninstall_refuses_concurrent_lifecycle_operation: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# Every tier can issue `compose down --remove-orphans`, so all three container
# ownership labels must match before tiers 1 and 2 may reach that mutation.
for tier in 1 2; do
  for label in project working-dir config-files; do
    new_env
    case "$label" in
      project)
        out="$(STUB_LABEL_PROJECT=foreign-project run_un --repo "$CLONE" --tier "$tier" --yes)"; rc=$? ;;
      working-dir)
        out="$(STUB_LABEL_WORKING_DIR="$ROOT/foreign-install" run_un --repo "$CLONE" --tier "$tier" --yes)"; rc=$? ;;
      config-files)
        out="$(STUB_LABEL_CONFIG_FILES="$ROOT/foreign-compose.yml" run_un --repo "$CLONE" --tier "$tier" --yes)"; rc=$? ;;
    esac
    if [ "$rc" -ne 0 ] && ! log_has 'compose .*down' && has "$out" 'ownership\|label\|refus'; then
      pass "tier${tier}_refuses_foreign_${label}_label"
    else
      check_fail "tier${tier}_refuses_foreign_${label}_label: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
    fi
  done
done

# A refusal before the first service mutation must clear its detached holder so
# the exact same uninstall can be retried immediately.
new_env
out="$(STUB_LABEL_PROJECT=foreign-project run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && [ ! -e "$BK/.lifecycle/operation.state" ] \
   && [ ! -e "$BK/.lifecycle/host.reservation" ]; then
  retry_out="$(run_un --repo "$CLONE" --tier 1 --yes)"; retry_rc=$?
else
  retry_out=""; retry_rc=99
fi
if [ "$retry_rc" -eq 0 ]; then
  pass "pre-mutation uninstall refusal clears its lifecycle holder for retry"
else
  check_fail "pre-mutation uninstall cleanup: rc=$rc retry=$retry_rc out=<<<$out>>> retry_out=<<<$retry_out>>>"
fi

# Once requester shutdown is attempted, an uncertain failure retains the exact
# operation identity. A retry must reactivate that identity and finish cleanup.
new_env
out="$(STUB_FAIL_REQUESTER_STOP_ONCE_FILE="$REQUESTER_STOP_FAIL_FILE" \
  run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
retained_id="$(cat "$BK/.lifecycle/operation.state" 2>/dev/null || true)"
retry_out="$(STUB_FAIL_REQUESTER_STOP_ONCE_FILE="$REQUESTER_STOP_FAIL_FILE" \
  run_un --repo "$CLONE" --tier 1 --yes)"; retry_rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$retained_id" | grep -Eq '^uninstall:[0-9a-f]{32}$' \
   && [ "$retry_rc" -eq 0 ] && [ ! -e "$BK/.lifecycle/operation.state" ] \
   && grep -qF "lifecycle reserve-host ${retained_id/:/ }" "$DOCKER_LOG"; then
  pass "requester-stop failure retains one uninstall identity and retry completes it"
else
  check_fail "requester-stop recovery: rc=$rc retained=$retained_id retry=$retry_rc out=<<<$out>>> retry_out=<<<$retry_out>>> log=$(cat "$DOCKER_LOG")"
fi

# The proof must inspect the complete project-labelled container set, not stop
# after one matching container.
new_env
out="$(STUB_COMPOSE_CONTAINER_ID=$'cid-managed\ncid-foreign' \
  STUB_SECOND_CONTAINER_FOREIGN=1 run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has 'compose .*down'; then
  pass "tier1_refuses_when_any_project_container_is_foreign"
else
  check_fail "tier1_refuses_when_any_project_container_is_foreign: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# Empty and unreadable ownership metadata are not evidence. Selected project
# containers must fail closed before either ordinary teardown tier.
new_env
out="$(STUB_MISSING_LABEL=config-files run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has 'compose .*down'; then
  pass "tier1_refuses_missing_compose_ownership_metadata"
else
  check_fail "tier1_refuses_missing_compose_ownership_metadata: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

new_env
out="$(STUB_INSPECT_FAIL=1 run_un --repo "$CLONE" --tier 2 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has 'compose .*down'; then
  pass "tier2_refuses_uninspectable_compose_ownership_metadata"
else
  check_fail "tier2_refuses_uninspectable_compose_ownership_metadata: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# The pre-prompt proof is not a lease. A changed label must be caught by the
# second proof immediately before down; deleting that proof makes these fail.
for tier in 1 2; do
  new_env
  out="$(STUB_REVERIFY_FOREIGN=1 run_un --repo "$CLONE" --tier "$tier" --yes)"; rc=$?
  if [ "$rc" -ne 0 ] && ! log_has 'compose .*down' && has "$out" 'ownership\|label\|refus'; then
    pass "tier${tier}_revalidates_ownership_immediately_before_teardown"
  else
    check_fail "tier${tier}_revalidates_ownership_immediately_before_teardown: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
  fi
done

# Tiers 1/2 remain idempotent only when no selected containers or project
# network remain. A leftover project network is still a Compose mutation target.
for tier in 1 2; do
  new_env
  out="$(STUB_NO_PROJECT_CONTAINERS=1 run_un --repo "$CLONE" --tier "$tier" --yes)"; rc=$?
  if [ "$rc" -eq 0 ] && log_has 'compose .* down'; then
    pass "tier${tier}_already_absent_stack_remains_idempotent"
  else
    check_fail "tier${tier}_already_absent_stack_remains_idempotent: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
  fi
done

new_env
out="$(STUB_NO_PROJECT_CONTAINERS=1 STUB_PROJECT_NETWORK=1 run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has 'compose .*down'; then
  pass "tier1_refuses_unowned_remaining_project_network"
else
  check_fail "tier1_refuses_unowned_remaining_project_network: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# Volume deletion additionally requires live Compose ownership evidence from the
# canonical project. A matching typed name cannot bless a foreign container.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(STUB_LABEL_PROJECT=foreign-project run_un --stdin "$proj" --repo "$CLONE" --tier 3 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has 'compose .*down' && has "$out" 'ownership\|label\|refus'; then
  pass "tier3_refuses_foreign_compose_labels: no volume-removing down"
else
  check_fail "tier3_refuses_foreign_compose_labels: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# Ownership can change while the operator is reading prompts. The destructive
# action must repeat the live proof immediately before passing --volumes.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(STUB_REVERIFY_FOREIGN=1 run_un --stdin "$proj" --repo "$CLONE" --tier 3 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has 'compose .*down' && has "$out" 'ownership\|label\|refus'; then
  pass "tier3_revalidates_ownership_immediately_before_volume_teardown"
else
  check_fail "tier3_revalidates_ownership_immediately_before_volume_teardown: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# A project-labelled volume whose logical Compose name is not declared by this
# repository is outside the deletion allowlist and must stop the operation.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(STUB_VOLUME_NAMES="${proj}_rogue" STUB_VOLUME_LOGICAL=rogue \
  run_un --stdin "$proj" --repo "$CLONE" --tier 3 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has 'compose .*down' && has "$out" 'volume\|allow\|declared\|refus'; then
  pass "tier3_refuses_undeclared_project_volume: exact declared-volume allowlist enforced"
else
  check_fail "tier3_refuses_undeclared_project_volume: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# =============================================================================
# 3. Docker daemon absent: all tiers refuse (exit 3) + print an orphan inventory.
# =============================================================================
new_env; STUB_NO_DAEMON=1
out="$(run_un --repo "$CLONE" --tier 2 --yes)"; rc=$?
STUB_NO_DAEMON=0
if [ "$rc" -eq 3 ] && has "$out" 'postgres_data' && has "$out" 'compose project' \
   && ! log_has 'lifecycle '; then
  pass "docker_absent_all_tiers_refuse_exit3_with_inventory: exit 3 + orphan inventory"
else
  check_fail "docker_absent_all_tiers_refuse_exit3_with_inventory: rc=$rc out=<<<$out>>>"
fi
mutation_log_empty "docker_absent_all_tiers_refuse_exit3_with_inventory: empty mutation log"

# =============================================================================
# 4. Dry run mutates nothing, and enumerates the same set it would remove.
# =============================================================================
new_env
out="$(run_un --repo "$CLONE" --dry-run --tier 4 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && has "$out" '^PLAN ' && [ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && [ -f "$CLONE/.env" ]; then
  pass "dry_run_mutates_nothing: PLAN lines printed, secrets/ and .env intact"
else
  check_fail "dry_run_mutates_nothing: rc=$rc secrets=$([ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && echo yes) out=<<<$out>>>"
fi
mutation_log_empty "dry_run_mutates_nothing: empty docker mutation log"

# dry-run parity at tier 2 (no conditional gates): PLAN set == real DONE set.
new_env
plan="$(run_un --repo "$CLONE" --dry-run --tier 2 --yes | grep '^PLAN ' | sed 's/^PLAN //' | sort)"
new_env
done_set="$(run_un --repo "$CLONE" --tier 2 --yes | grep '^DONE ' | sed 's/^DONE //' | sort)"
if [ -n "$plan" ] && [ "$plan" = "$done_set" ]; then
  pass "dry_run_parity: tier-2 enumeration equals the real mutation set"
else
  check_fail "dry_run_parity: plan=<<<$plan>>> done=<<<$done_set>>>"
fi

# =============================================================================
# 5. Tier semantics.
# =============================================================================
new_env
out="$(run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && log_has 'compose .* down' && ! log_has 'rmi ' && ! log_has '--volumes'; then
  pass "tier1_only_compose_down: containers/network only, no image or volume removal"
else
  check_fail "tier1_only_compose_down: rc=$rc log=$(cat "$DOCKER_LOG")"
fi

new_env
run_un --repo "$CLONE" --tier 2 --yes >/dev/null; rc=$?
rmi_lines="$(grep 'rmi ' "$DOCKER_LOG" 2>/dev/null)"
ok_all=1
for r in "${APP_REFS[@]}"; do log_has "rmi -f $r" || ok_all=0; done
n_rmi="$(printf '%s\n' "$rmi_lines" | grep -c 'rmi ')"
if [ "$ok_all" -eq 1 ] && [ "$n_rmi" -eq "${#APP_REFS[@]}" ] \
   && ! printf '%s' "$rmi_lines" | grep -qE 'postgres|ollama|qdrant|caddy'; then
  pass "tier2_removes_exactly_the_ghcr_images: the four app refs and nothing else"
else
  check_fail "tier2_removes_exactly_the_ghcr_images: n=$n_rmi lines=<<<$rmi_lines>>>"
fi

# Installations created before JARVIS_VERSION was persisted still resolve the
# exact checkout version. This fallback is read-only, including in dry-run mode.
new_env
grep -v '^JARVIS_VERSION=' "$CLONE/.env" > "$CLONE/.env.next"
mv "$CLONE/.env.next" "$CLONE/.env"
before_env="$(cat "$CLONE/.env")"
out="$(run_un --repo "$CLONE" --dry-run --tier 2 --yes)"; rc=$?
fallback_ok=1
for r in "${APP_REFS[@]}"; do has "$out" "PLAN image $r" || fallback_ok=0; done
if [ "$rc" -eq 0 ] && [ "$fallback_ok" -eq 1 ] \
   && [ "$(cat "$CLONE/.env")" = "$before_env" ]; then
  pass "legacy_missing_version_pin_uses_checkout_metadata_without_mutating_env"
else
  check_fail "legacy_missing_version_pin: rc=$rc env=$(cat "$CLONE/.env") out=<<<$out>>>"
fi

# A malformed durable pin is not converted into an arbitrary Docker image ref.
new_env
printf 'JARVIS_VERSION=not-a-version\nTORCH_VARIANT_SUFFIX=-cuda\n' > "$CLONE/.env"
out="$(run_un --repo "$CLONE" --dry-run --tier 2 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && has "$out" 'application version is missing or invalid' \
   && ! has "$out" '^PLAN '; then
  pass "invalid_application_version_refuses_before_uninstall_plan"
else
  check_fail "invalid_application_version_refusal: rc=$rc out=<<<$out>>>"
fi

# tier 3 requires typing the compose project name; a wrong name refuses.
new_env
out="$(run_un --stdin 'wrong-name' --repo "$CLONE" --tier 3 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has '--volumes'; then
  pass "tier3_requires_typed_project_name: wrong name -> refuse, no --volumes"
else
  check_fail "tier3_requires_typed_project_name: rc=$rc log=$(cat "$DOCKER_LOG")"
fi

# When the running stack is about to lose its data volumes, an accepted backup
# offer runs backup.sh inside the postgres-backup service so Docker Secrets and
# the backup volumes are available. A successful backup proceeds to the typed
# deletion gate.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(STUB_STACK_UP=1 run_un --stdin "$(printf 'y\ny\n%s\n' "$proj")" \
  --repo "$CLONE" --tier 3)"; rc=$?
backup_line="$(grep 'compose .* run ' "$DOCKER_LOG" 2>/dev/null || true)"
if [ "$rc" -eq 0 ] \
   && printf '%s' "$backup_line" | grep -q -- '--entrypoint /usr/local/bin/backup.sh' \
   && printf '%s' "$backup_line" | grep -q -- 'postgres-backup' \
   && log_has 'compose .* down' && log_has '--volumes'; then
  pass "tier3_requested_backup_runs_in_the_backup_sidecar_before_volume_deletion"
else
  check_fail "tier3 requested backup contract: rc=$rc backup=<<<$backup_line>>> log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# A requested backup is a safety gate, not a best-effort warning. Its failure
# stops before the project-name prompt can authorize destructive teardown.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(STUB_STACK_UP=1 STUB_FAIL_BACKUP=1 run_un \
  --stdin "$(printf 'y\ny\n%s\n' "$proj")" --repo "$CLONE" --tier 3)"; rc=$?
if [ "$rc" -ne 0 ] && has "$out" 'Backup did not complete' \
   && ! log_has 'compose .* down' && ! log_has '--volumes'; then
  pass "tier3_requested_backup_failure_refuses_destructive_teardown"
else
  check_fail "tier3 failed backup gate: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi

# backup.sh reports lock contention as a successful no-op. Treat that message
# as incomplete for uninstall purposes: stopping the sidecar could otherwise
# interrupt the real backup and immediately delete its destination volume.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(STUB_STACK_UP=1 STUB_BACKUP_BUSY=1 run_un \
  --stdin "$(printf 'y\ny\n%s\n' "$proj")" --repo "$CLONE" --tier 3)"; rc=$?
if [ "$rc" -ne 0 ] && has "$out" 'Another backup was still running' \
   && ! log_has 'compose .* down' && ! log_has '--volumes'; then
  pass "tier3_busy_backup_refuses_destructive_teardown_until_backup_finishes"
else
  check_fail "tier3 busy backup gate: rc=$rc log=$(cat "$DOCKER_LOG") out=<<<$out>>>"
fi
# correct name proceeds to a volume-removing down.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(run_un --stdin "$proj" --repo "$CLONE" --tier 3 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && log_has 'compose .* down' && log_has '--volumes'; then
  pass "tier3_correct_name_removes_volumes: typed project name -> down --volumes"
else
  check_fail "tier3_correct_name_removes_volumes: rc=$rc log=$(cat "$DOCKER_LOG")"
fi

# --keep-data caps at tier 2 even when tier 4 is requested.
new_env
out="$(run_un --repo "$CLONE" --tier 4 --keep-data --yes)"; rc=$?
if [ "$rc" -eq 0 ] && ! log_has '--volumes' && log_has "rmi -f ${APP_REFS[0]}" \
   && [ -d "$CLONE/secrets" ] && [ -f "$CLONE/.env" ]; then
  pass "keep_data_caps_at_tier2: no volume/purge action, data + files intact"
else
  check_fail "keep_data_caps_at_tier2: rc=$rc secrets=$([ -d "$CLONE/secrets" ] && echo yes) log=$(cat "$DOCKER_LOG")"
fi

# --yes without an explicit --tier is a usage error.
new_env
out="$(run_un --repo "$CLONE" --yes)"; rc=$?
if [ "$rc" -eq 2 ]; then pass "yes_requires_tier: --yes with no --tier -> usage exit 2"; else check_fail "yes_requires_tier: rc=$rc out=$out"; fi
mutation_log_empty "yes_requires_tier: empty mutation log"

# =============================================================================
# 6. Purge-tier destructive gates (mandatory even under --yes/--all).
# =============================================================================
# no export path + wrong phrase -> refuse, secrets/ untouched.
new_env
out="$(run_un --stdin "$(printf '%s\n\nnope' "$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')")" \
  --repo "$CLONE" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && [ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && ! log_has 'rmi -f postgres'; then
  pass "purge_requires_key_export_or_typed_phrase: declined export + wrong phrase -> refuse, secrets intact"
else
  check_fail "purge_requires_key_export_or_typed_phrase(refuse): rc=$rc secrets=$([ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && echo yes)"
fi

# export path given -> the key copy PRECEDES any file removal in the action log.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT="$ROOT/exported.$RANDOM.key"
# stdin: tier-3 project name, key export path, then Y to every third-party image.
out="$(run_un --stdin "$(printf '%s\n%s\ny\ny\ny\ny' "$proj" "$EXPORT")" --repo "$CLONE" --tier 4 --yes)"; rc=$?
key_idx="$(printf '%s\n' "$out" | grep -n 'DONE key-export' | head -1 | cut -d: -f1)"
rm_idx="$(printf '%s\n' "$out" | grep -n 'DONE file ' | head -1 | cut -d: -f1)"
if [ -f "$EXPORT" ] && [ -n "$key_idx" ] && [ -n "$rm_idx" ] && [ "$key_idx" -lt "$rm_idx" ]; then
  pass "purge_key_export_precedes_rm: key copied out before any file removal"
else
  check_fail "purge_key_export_precedes_rm: export=$([ -f "$EXPORT" ] && echo yes) key_idx=$key_idx rm_idx=$rm_idx out=<<<$out>>>"
fi

# export path inside the clone (or secrets/) is refused; no key copy lands there.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
INSIDE="$CLONE/secrets/exported.key"
out="$(run_un --stdin "$(printf '%s\n%s' "$proj" "$INSIDE")" --repo "$CLONE" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && [ ! -f "$INSIDE" ] && has "$out" 'inside the clone'; then
  pass "purge_key_export_refuses_in_clone_path: in-clone export path re-prompted/refused, no copy there"
else
  check_fail "purge_key_export_refuses_in_clone_path: rc=$rc inside=$([ -f "$INSIDE" ] && echo yes) out=<<<$out>>>"
fi

# An apparently outside path may traverse a symlink back into the clone. This
# must remain a refusal even when a `realpath` command exists but cannot provide
# GNU's -m behavior (the suite's stub always exits 64).
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
OUTSIDE_LINK="$ROOT/apparently-outside.$RANDOM"
ln -s "$CLONE/secrets" "$OUTSIDE_LINK"
SYMLINK_EXPORT="$OUTSIDE_LINK/exported.key"
out="$(run_un --stdin "$(printf '%s\n%s' "$proj" "$SYMLINK_EXPORT")" --repo "$CLONE" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && [ ! -f "$SYMLINK_EXPORT" ] \
   && [ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && has "$out" 'inside the clone'; then
  pass "purge_key_export_resolves_outside_symlink_into_clone_without_gnu_realpath"
else
  check_fail "purge_key_export_resolves_outside_symlink_into_clone_without_gnu_realpath: rc=$rc out=<<<$out>>>"
fi

# --all with a closed stdin cannot satisfy the typed purge gate -> refuse.
new_env
out="$(run_un --repo "$CLONE" --all)"; rc=$?   # empty stdin
if [ "$rc" -ne 0 ] && [ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && ! log_has 'rmi -f postgres'; then
  pass "yes_all_still_requires_typed_purge_confirmation: closed stdin -> refuse, secrets intact"
else
  check_fail "yes_all_still_requires_typed_purge_confirmation: rc=$rc secrets=$([ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && echo yes)"
fi

# --all never auto-confirms the per-image third-party removals: with the typed
# gates satisfied but the confirms hitting EOF, no third-party image is removed.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT2="$ROOT/exported2.$RANDOM.key"
out="$(run_un --stdin "$(printf '%s\n%s' "$proj" "$EXPORT2")" --repo "$CLONE" --all)"; rc=$?
if ! printf '%s\n' "$(cat "$DOCKER_LOG")" | grep -qE 'rmi -f (postgres|ollama|qdrant|caddy)' \
   && has "$out" 'Keeping shared third-party'; then
  pass "yes_all_never_skips_third_party_image_confirms: unconfirmed third-party images kept, named"
else
  check_fail "yes_all_never_skips_third_party_image_confirms: log=$(grep rmi "$DOCKER_LOG"); out=<<<$out>>>"
fi

# a missing image during rmi (partial prior run, or a declared-but-never-pulled
# variant) must be skipped, not abort teardown after the volumes are already gone.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORTF="$ROOT/exportfail.$RANDOM.key"
out="$(STUB_FAIL_RMI="${APP_REFS[0]}" run_un --stdin "$(printf '%s\n%s\ny\ny\ny\ny' "$proj" "$EXPORTF")" --repo "$CLONE" --tier 4 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && has "$out" "not present, skipping: ${APP_REFS[0]}" && [ ! -d "$CLONE" ]; then
  pass "rmi_missing_image_completes_teardown: absent image skipped, teardown finishes (clone removed), exit 0"
else
  check_fail "rmi_missing_image_completes_teardown: rc=$rc clone=$([ -d "$CLONE" ] && echo present) out=<<<$out>>>"
fi

# =============================================================================
# 7. Registry line + shim removed only when this is the last install.
# =============================================================================
# last install: installs holds only this clone -> line removed AND shim removed.
new_env
CANON="$(realpath "$CLONE")"
printf '%s\n' "$CANON" > "$CFG/installs"
printf '#shim\n' > "$BIN/jarvis-research"; chmod +x "$BIN/jarvis-research"
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT3="$ROOT/exported3.$RANDOM.key"
run_un --stdin "$(printf '%s\n%s\nn\nn\nn\nn' "$proj" "$EXPORT3")" --repo "$CLONE" --tier 4 --yes >/dev/null 2>&1
if ! grep -qxF "$CANON" "$CFG/installs" 2>/dev/null && [ ! -e "$BIN/jarvis-research" ]; then
  pass "tier4_removes_state_line_and_shim_when_last: registry line gone, shim gone"
else
  check_fail "tier4_removes_state_line_and_shim_when_last: installs=$(cat "$CFG/installs" 2>/dev/null) shim=$([ -e "$BIN/jarvis-research" ] && echo present)"
fi

# not last: another install remains -> line removed but the shim stays.
new_env
CANON="$(realpath "$CLONE")"
OTHER="$ROOT/otherinstall.$RANDOM"; mkdir -p "$OTHER"
printf '%s\n%s\n' "$CANON" "$OTHER" > "$CFG/installs"
printf '#shim\n' > "$BIN/jarvis-research"; chmod +x "$BIN/jarvis-research"
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT4="$ROOT/exported4.$RANDOM.key"
run_un --stdin "$(printf '%s\n%s\nn\nn\nn\nn' "$proj" "$EXPORT4")" --repo "$CLONE" --tier 4 --yes >/dev/null 2>&1
if ! grep -qxF "$CANON" "$CFG/installs" 2>/dev/null && grep -qxF "$OTHER" "$CFG/installs" 2>/dev/null \
   && [ -e "$BIN/jarvis-research" ]; then
  pass "tier4_keeps_shim_when_another_install_remains: line removed, other kept, shim retained"
else
  check_fail "tier4_keeps_shim_when_another_install_remains: installs=$(cat "$CFG/installs" 2>/dev/null) shim=$([ -e "$BIN/jarvis-research" ] && echo present)"
fi

# =============================================================================
if [ "$fail" -ne 0 ]; then
  printf '\nuninstall: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nuninstall: all %s checks passed\n' "$pass_n"
