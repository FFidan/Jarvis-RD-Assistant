#!/usr/bin/env bash
# Rotate JARVIS_CONFIG_KEY with a validated, crash-resumable maintenance window.
set -euo pipefail

YES=0
case "${1:-}" in
  "") ;;
  --yes) YES=1 ;;
  -h|--help)
    printf '%s\n' 'Usage: bash scripts/rotate-config-key.sh [--yes]'
    printf '%s\n' 'Validates and rotates encrypted Settings rows, then restarts affected services.'
    exit 0
    ;;
  *) printf 'ERROR: unknown option: %s\n' "$1" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${JARVIS_ROTATION_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
cd "$REPO_ROOT"
# shellcheck source=setup_lib.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/setup_lib.sh"

ACTIVE_KEY="secrets/jarvis_config_key.txt"
NEXT_KEY="secrets/jarvis_config_key_next.txt"
PREVIOUS_KEY="secrets/jarvis_config_key_previous.txt"
ENV_BACKUP=".env.pre-config-key-rotation.bak"
STATE_FILE="secrets/jarvis_config_key_rotation_state.txt"
COMMAND_LOCK="secrets/.jarvis_config_key_rotation.lock"
ROTATION_TOOL="$REPO_ROOT/scripts/rotate_config_key.py"
LIFECYCLE_HELPER="$REPO_ROOT/scripts/backup-lifecycle.sh"
SUCCESS=0
ROTATION_GUARD_ID=""
ROTATION_GUARD_ACTIVE=0
BACKUP_SERVICE_WAS_RUNNING=0
CANCEL_RESTART_SERVICES=0

COMPOSE_PROJECT=""
COMPOSE_CONFIG_LABEL=""
declare -a COMPOSE_FILES=()

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

# Serialize the whole host-side workflow before reading or changing any staged
# rotation state. Keep the lock file itself persistent so two processes can
# never end up locking different inodes during cleanup.
host_lock_rc=0
claim_host_lifecycle_lock "$REPO_ROOT" || host_lock_rc=$?
case "$host_lock_rc" in
  0) ;;
  3) fail "another JARVIS lifecycle operation is already running"; exit 1 ;;
  *) fail "the per-install lifecycle lock is unavailable or unsafe"; exit 1 ;;
esac
path_inside_repo() { case "$1/" in "$2"/*) return 0 ;; esac; return 1; }

# Resolve Compose only from this install's recorded .env. Explicit CLI selectors
# outrank .env and the caller's ambient COMPOSE_* variables are removed, so a
# maintenance command cannot be redirected to a sibling project.
init_compose_target() {
  local raw item candidate canon seen="" joined="" name
  local -a requested=()
  name="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env 2>/dev/null | head -1)"
  case "$name" in
    \"*\") name="${name#\"}"; name="${name%\"}" ;;
    \'*\') name="${name#\'}"; name="${name%\'}" ;;
  esac
  [ -n "$name" ] || name="$(basename "$REPO_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  printf '%s' "$name" | grep -Eq '^[a-z0-9][a-z0-9_-]*$' \
    || fail "invalid COMPOSE_PROJECT_NAME '${name}' in .env"
  COMPOSE_PROJECT="$name"

  raw="$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | head -1)"
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
    \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
  esac
  if [ -z "$raw" ]; then
    raw=docker-compose.yml
    [ ! -f "$REPO_ROOT/docker-compose.override.yml" ] \
      || raw="${raw}:docker-compose.override.yml"
  fi
  IFS=: read -r -a requested <<< "$raw"
  for item in "${requested[@]}"; do
    [ -n "$item" ] || fail "COMPOSE_FILE contains an empty entry"
    case "$item" in /*) candidate="$item" ;; *) candidate="$REPO_ROOT/$item" ;; esac
    canon="$(canonical_path_portable "$candidate" 2>/dev/null || true)"
    if [ -z "$canon" ] || [ ! -f "$canon" ] || ! path_inside_repo "$canon" "$REPO_ROOT"; then
      fail "Compose file '${item}' is missing or outside this install"
    fi
    if printf '%s\n' "$seen" | grep -qxF "$canon"; then
      fail "Compose file '${item}' is listed more than once"
    fi
    COMPOSE_FILES+=("$canon")
    seen="${seen}${canon}"$'\n'
    joined="${joined:+${joined},}${canon}"
  done
  [ "${COMPOSE_FILES[0]:-}" = "$REPO_ROOT/docker-compose.yml" ] \
    || fail "COMPOSE_FILE must start with this install's docker-compose.yml"
  COMPOSE_CONFIG_LABEL="$joined"
}

compose_for_install() {
  local -a cmd=(docker compose --project-directory "$REPO_ROOT" --env-file "$REPO_ROOT/.env" -p "$COMPOSE_PROJECT")
  local file
  for file in "${COMPOSE_FILES[@]}"; do cmd+=(-f "$file"); done
  env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
      -u COMPOSE_PATH_SEPARATOR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
      "${cmd[@]}" "$@" 7>&-
}

verify_compose_owner() {
  local cid labels project workdir configs
  cid="$(compose_for_install ps -q postgres 2>/dev/null | head -1 || true)"
  [ -n "$cid" ] || fail "cannot verify this install's running postgres container"
  labels="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}|{{ index .Config.Labels "com.docker.compose.project.working_dir" }}|{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$cid" 2>/dev/null || true)"
  IFS='|' read -r project workdir configs <<< "$labels"
  if [ "$project" != "$COMPOSE_PROJECT" ] \
     || [ "$workdir" != "$REPO_ROOT" ] \
     || [ "$configs" != "$COMPOSE_CONFIG_LABEL" ]; then
    fail "Compose ownership does not match this JARVIS install; refusing rotation"
  fi
}

volume_helper() {
  compose_for_install run --rm --no-deps --entrypoint bash \
    --volume "$LIFECYCLE_HELPER:/tmp/backup-lifecycle.sh:ro" \
    postgres-backup /tmp/backup-lifecycle.sh "$@" 7>&- 8>&-
}

write_state() {
  local phase="$1" tmp
  tmp="$(mktemp "${STATE_FILE}.XXXXXX")"
  printf '%s\nbackup_service_was_running=%s\nguard_id=%s\ncancel_restart_services=%s\n' \
    "$phase" "$BACKUP_SERVICE_WAS_RUNNING" "$ROTATION_GUARD_ID" \
    "$CANCEL_RESTART_SERVICES" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$STATE_FILE"
}

current_phase() {
  if [ -s "$STATE_FILE" ]; then
    head -n 1 "$STATE_FILE" | tr -d '\r\n'
  else
    printf 'new'
  fi
}

restart_old_services_after_failure() {
  printf '%s\n' 'Rotation did not change the database. Restarting services with the existing key.' >&2
  local -a services=(paper_ingestion learning_engine)
  [ "$BACKUP_SERVICE_WAS_RUNNING" -ne 1 ] || services+=(postgres-backup)
  compose_for_install up -d "${services[@]}" >&2 || true
}

start_rotation_guard() {
  local timeout attempts interval current_id="" reservation_action=""
  local helper_container="" helper_state="" inspect_output="" inspect_rc=0
  local wait_output="" wait_output_file="" wait_warning=0
  [ "$ROTATION_GUARD_ACTIVE" -ne 1 ] || return 0
  timeout="${JARVIS_ROTATION_GUARD_TIMEOUT:-21600}"
  attempts="${JARVIS_ROTATION_GUARD_READY_ATTEMPTS:-100}"
  interval="${JARVIS_ROTATION_GUARD_READY_INTERVAL:-0.1}"
  printf '%s' "$timeout" | grep -Eq '^[1-9][0-9]{0,5}$' \
    || fail "JARVIS_ROTATION_GUARD_TIMEOUT must be a positive integer"
  printf '%s' "$attempts" | grep -Eq '^[1-9][0-9]{0,5}$' \
    || fail "JARVIS_ROTATION_GUARD_READY_ATTEMPTS must be a positive integer"
  printf '%s' "$interval" \
    | grep -Eq '^(0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*(\.[0-9]+)?)$' \
    || fail "JARVIS_ROTATION_GUARD_READY_INTERVAL must be a positive number"

  if current_id="$(volume_helper current-rotation 2>/dev/null)"; then
    if [ -n "$ROTATION_GUARD_ID" ] && [ "$current_id" = "$ROTATION_GUARD_ID" ]; then
      ROTATION_GUARD_ACTIVE=1
      printf '%s\n' 'Adopting the existing backup maintenance guard.'
      return 0
    fi
    fail "another backup maintenance guard is active; inspect the rotation state before retrying"
    return 1
  fi

  if [ -z "$ROTATION_GUARD_ID" ]; then
    ROTATION_GUARD_ID="$(openssl rand -hex 16 2>/dev/null || true)"
    printf '%s' "$ROTATION_GUARD_ID" | grep -Eq '^[0-9a-f]{32}$' \
      || fail "could not generate the rotation guard identity"
    # The state record is the durable identity source. Retries must reuse it,
    # including while the detached helper is still waiting for a backup.
    write_state "$phase"
  fi
  printf '%s' "$ROTATION_GUARD_ID" | grep -Eq '^[0-9a-f]{32}$' \
    || fail "rotation state has no valid guard identity"
  reservation_action="$(volume_helper reserve-rotation "$ROTATION_GUARD_ID")" \
    || fail "could not reserve the backup maintenance guard identity"
  case "$reservation_action" in
    adopt)
      printf '%s\n' 'Adopting the existing backup maintenance guard.'
      ;;
    launch)
      helper_container="$(compose_for_install run --rm --no-deps -d --entrypoint bash \
        --volume "$LIFECYCLE_HELPER:/tmp/backup-lifecycle.sh:ro" \
        postgres-backup /tmp/backup-lifecycle.sh hold-rotation \
        "$ROTATION_GUARD_ID" "$timeout" 7>&- 8>&-)" \
        || fail "could not start the backup maintenance guard helper"
      ;;
    *) fail "backup maintenance guard reservation returned an unknown state" ;;
  esac
  printf 'An active backup may still be finishing; waiting up to %s seconds.\n' "$timeout"
  wait_output_file="$(mktemp)" || fail "could not create backup-guard monitor state"
  while true; do
    if volume_helper wait-rotation \
        "$ROTATION_GUARD_ID" "$attempts" "$interval" \
        >"$wait_output_file" 2>&1; then
      rm -f "$wait_output_file"
      ROTATION_GUARD_ACTIVE=1
      return 0
    fi
    wait_output="$(cat "$wait_output_file" 2>/dev/null || true)"
    : > "$wait_output_file"
    if printf '%s\n' "$wait_output" \
        | grep -qF 'ERROR: rotation reservation owner stopped before guard activation'; then
      rm -f "$wait_output_file"
      fail "backup maintenance guard timed out before activation; re-run the rotation"
      return 1
    fi
    if printf '%s\n' "$wait_output" \
        | grep -qF 'ERROR: rotation reservation owner was not observed before guard activation'; then
      # A freshly launched container might still be between Docker startup and
      # taking the reservation. Accept failure only after Docker proves that
      # exact container can no longer run; otherwise keep monitoring it.
      if [ -n "$helper_container" ] \
         && printf '%s' "$helper_container" | grep -Eq '^[0-9a-f]{12,64}$'; then
        inspect_rc=0
        inspect_output="$(docker inspect --format '{{.State.Status}}' \
          "$helper_container" 2>&1 7>&-)" || inspect_rc=$?
        if [ "$inspect_rc" -eq 0 ]; then
          helper_state="$inspect_output"
          case "$helper_state" in
            exited|dead) rm -f "$wait_output_file"; fail "backup maintenance guard helper stopped before activation; re-run the rotation"; return 1 ;;
            *) sleep 1; continue ;;
          esac
        fi
        if printf '%s\n' "$inspect_output" | grep -q 'No such'; then
          rm -f "$wait_output_file"
          fail "backup maintenance guard helper stopped before activation; re-run the rotation"
          return 1
        fi
        sleep 1
        continue
      fi
      # `adopt` is returned only while an owner is locked, so an unobserved
      # owner after that handoff is already gone and cannot activate later.
      if [ "$reservation_action" = adopt ]; then
        rm -f "$wait_output_file"
        fail "backup maintenance guard helper stopped before activation; re-run the rotation"
        return 1
      fi
    fi
    # A Compose/daemon transport failure says nothing about the detached owner.
    # Keep waiting instead of returning while that owner could still activate.
    if [ "$wait_warning" -eq 0 ]; then
      printf '%s\n' 'Waiting for Docker to resume backup-guard monitoring...' >&2
      wait_warning=1
    fi
    sleep 1
  done
}

finish_rotation_guard() {
  local action="$1" attempt=0
  [ "$ROTATION_GUARD_ACTIVE" -eq 1 ] || return 0
  volume_helper release-rotation "$ROTATION_GUARD_ID" "$action" >/dev/null \
    || return 1
  while [ "$attempt" -lt 30 ]; do
    if volume_helper rotation-release-complete \
         "$ROTATION_GUARD_ID" "$action" >/dev/null 2>&1; then
      ROTATION_GUARD_ACTIVE=0
      return 0
    fi
    sleep 0.1
    attempt=$((attempt + 1))
  done
  return 1
}

on_exit() {
  local rc=$? phase
  if [ "$rc" -ne 0 ] && [ "$SUCCESS" -ne 1 ]; then
    phase="$(current_phase 2>/dev/null || printf unknown)"
    case "$phase" in
      prepared)
        restart_old_services_after_failure
        if ! finish_rotation_guard "clear"; then
          printf '%s\n' 'The backup maintenance guard could not be cleared; backups remain blocked.' >&2
        fi
        ;;
      quiescing)
        # This phase is persisted before service stop and before any database
        # mutation. Restore the old-key service set; the next run will finish
        # the durable cancellation transaction.
        restart_old_services_after_failure
        if ! finish_rotation_guard "clear"; then
          printf '%s\n' 'The backup maintenance guard could not be cleared; backups remain blocked.' >&2
        fi
        printf '%s\n' 'Pre-mutation cancellation is incomplete; re-run config-key rotation.' >&2
        ;;
      cancelling)
        [ "$CANCEL_RESTART_SERVICES" -ne 1 ] || restart_old_services_after_failure
        if [ "$ROTATION_GUARD_ACTIVE" -eq 1 ]; then
          if ! finish_rotation_guard retain; then
            printf '%s\n' 'The backup maintenance guard could not be released; inspect the postgres-backup helper container.' >&2
          fi
        fi
        printf '%s\n' 'Rotation cancellation is incomplete; re-run config-key rotation.' >&2
        ;;
      mutation-unknown|database-rotated|promoted)
        # Once a commit is possible, no affected service may run until the key
        # state is deterministic. Retain the durable sentinel even after the
        # mutex is released so every backup mode remains fail-closed.
        compose_for_install stop paper_ingestion learning_engine postgres-backup >&2 || true
        if [ "$ROTATION_GUARD_ACTIVE" -eq 1 ]; then
          if ! finish_rotation_guard retain; then
            printf '%s\n' 'The backup maintenance guard could not be released; inspect the postgres-backup helper container.' >&2
          fi
        fi
        printf '%s\n' 'Rotation needs attention. Data and staged recovery files were kept.' >&2
        printf '%s\n' 'Re-run: bash scripts/rotate-config-key.sh --yes' >&2
        ;;
      finalizing)
        # Application services already passed their post-promotion health
        # checks. Keep backups blocked if cleanup itself fails, but do not take
        # the healthy application back down.
        if [ "$ROTATION_GUARD_ACTIVE" -eq 1 ]; then
          if ! finish_rotation_guard retain; then
            printf '%s\n' 'The backup maintenance guard could not be released; inspect the postgres-backup helper container.' >&2
          fi
        fi
        printf '%s\n' 'Rotation finalization is incomplete; inspect the reported error before allowing backups.' >&2
        printf '%s\n' 'Re-run: bash scripts/rotate-config-key.sh --yes' >&2
        ;;
    esac
  fi
  exit "$rc"
}
trap on_exit EXIT

run_rotation_tool() {
  local old_file="$1" new_file="$2"
  shift 2
  compose_for_install run --rm --no-deps \
    -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
    -e OLD_JARVIS_CONFIG_KEY_FILE=/run/rotation/old \
    -e NEW_JARVIS_CONFIG_KEY_FILE=/run/rotation/new \
    --volume "$ROTATION_TOOL:/tmp/rotate_config_key.py:ro" \
    --volume "$old_file:/run/rotation/old:ro" \
    --volume "$new_file:/run/rotation/new:ro" \
    paper_ingestion python /tmp/rotate_config_key.py "$@"
}

write_env_key_from_file() {
  local value_file="$1" tmp
  tmp="$(mktemp "${REPO_ROOT}/.env.rotation.XXXXXX")"
  if ! awk -v k="JARVIS_CONFIG_KEY" -v value_file="$value_file" '
    BEGIN {
      if ((getline replacement < value_file) != 1 || replacement == "") exit 2
      close(value_file)
    }
    index($0, k "=") == 1 {
      if (!seen) { print k "=" replacement; seen = 1 }
      next
    }
    { print }
    END { if (!seen) print k "=" replacement }
  ' .env > "$tmp"; then
    rm -f "$tmp"
    fail "could not stage the .env key update"
  fi
  chmod 600 "$tmp"
  mv "$tmp" .env
}

wait_service_healthy() {
  local service="$1" attempt=0 container status
  local attempts="${JARVIS_ROTATION_HEALTH_ATTEMPTS:-60}"
  local interval="${JARVIS_ROTATION_HEALTH_INTERVAL:-2}"
  while [ "$attempt" -lt "$attempts" ]; do
    container="$(compose_for_install ps -q "$service" 2>/dev/null || true)"
    if [ -n "$container" ]; then
      status="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
      [ "$status" = "healthy" ] && return 0
    fi
    attempt=$((attempt + 1))
    sleep "$interval"
  done
  return 1
}

wait_service_running() {
  local service="$1" attempt=0 container status
  local attempts="${JARVIS_ROTATION_HEALTH_ATTEMPTS:-60}"
  local interval="${JARVIS_ROTATION_HEALTH_INTERVAL:-2}"
  while [ "$attempt" -lt "$attempts" ]; do
    container="$(compose_for_install ps -q "$service" 2>/dev/null || true)"
    if [ -n "$container" ]; then
      status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
      [ "$status" = running ] && return 0
    fi
    attempt=$((attempt + 1))
    sleep "$interval"
  done
  return 1
}

restart_old_services_for_cancellation() {
  local -a services=(paper_ingestion learning_engine)
  [ "$BACKUP_SERVICE_WAS_RUNNING" -ne 1 ] || services+=(postgres-backup)
  printf '%s\n' 'Restarting services with the unchanged config key...'
  compose_for_install up -d "${services[@]}"
  wait_service_healthy paper_ingestion \
    || fail "paper_ingestion did not recover after rotation cancellation"
  wait_service_healthy learning_engine \
    || fail "learning_engine did not recover after rotation cancellation"
  if [ "$BACKUP_SERVICE_WAS_RUNNING" -eq 1 ]; then
    wait_service_running postgres-backup \
      || fail "postgres-backup did not recover after rotation cancellation"
  fi
}

finalize_cancellation() {
  if [ "$CANCEL_RESTART_SERVICES" -eq 1 ]; then
    restart_old_services_for_cancellation
  fi
  # Keep the phase record until every other cleanup action succeeds. Missing
  # files are expected after a crash and rm -f makes each step replay-safe.
  rm -f -- "$NEXT_KEY" || fail "could not remove $NEXT_KEY during cancellation"
  rm -f -- "$PREVIOUS_KEY" || fail "could not remove $PREVIOUS_KEY during cancellation"
  rm -f -- "$ENV_BACKUP" || fail "could not remove $ENV_BACKUP during cancellation"
  finish_rotation_guard clear \
    || fail "could not clear the backup maintenance guard after cancellation"
  rm -f -- "$STATE_FILE" \
    || fail "could not remove $STATE_FILE; re-run rotation cancellation"
  SUCCESS=1
  printf '%s\n' 'Rotation cancelled; no data or active key changed.'
}

# A database transaction can commit even when Docker loses the command's
# success response. Exactly one key validating every encrypted row proves the
# outcome. Both or neither is ambiguous and must keep services and backups off.
reconcile_mutation_unknown() {
  local output marker state rows
  printf '%s\n' 'Determining whether the database committed the key rotation...'
  if ! output="$(run_rotation_tool "$ACTIVE_KEY" "$NEXT_KEY" --probe-state)"; then
    fail "could not inspect the database rotation state; affected services and backups remain stopped"
    return 1
  fi
  if [ "$(printf '%s\n' "$output" | grep -Ec '^JARVIS_ROTATION_STATE=(old|new|empty|ambiguous) ROWS=[0-9]+$')" -ne 1 ]; then
    fail "database rotation probe returned no trustworthy state; affected services and backups remain stopped"
    return 1
  fi
  marker="$(printf '%s\n' "$output" | grep -E '^JARVIS_ROTATION_STATE=(old|new|empty|ambiguous) ROWS=[0-9]+$')"
  state="${marker#JARVIS_ROTATION_STATE=}"
  state="${state%% *}"
  rows="${marker##*ROWS=}"
  case "$state" in
    old)
      [ "$rows" -ge 1 ] || fail "database rotation probe returned an invalid old-key state"
      CANCEL_RESTART_SERVICES=1
      write_state cancelling
      phase=cancelling
      fail "database rotation definitely did not commit; re-run to finish safe cancellation"
      ;;
    new)
      [ "$rows" -ge 1 ] || fail "database rotation probe returned an invalid new-key state"
      write_state database-rotated
      phase=database-rotated
      printf '%s\n' 'The database committed the rotation; continuing with the staged key.'
      ;;
    empty)
      [ "$rows" -eq 0 ] || fail "database rotation probe returned an invalid empty state"
      write_state database-rotated
      phase=database-rotated
      printf '%s\n' 'No encrypted Settings rows exist; continuing safely with the staged key.'
      ;;
    ambiguous)
      fail "database rotation outcome is ambiguous; affected services and backups remain stopped"
      ;;
  esac
}

[ -f .env ] || fail ".env is missing; run ./setup.sh first"
[ -s "$ACTIVE_KEY" ] || fail "$ACTIVE_KEY is missing or empty"
[ -f "$ROTATION_TOOL" ] || fail "$ROTATION_TOOL is missing"
[ -f "$LIFECYCLE_HELPER" ] || fail "$LIFECYCLE_HELPER is missing"
[ -f versions.env ] || fail "versions.env is missing; restore the managed install before rotating its key"
command -v docker >/dev/null 2>&1 || fail "Docker is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
docker info >/dev/null 2>&1 || fail "the Docker daemon is not reachable; start Docker and retry"
command -v openssl >/dev/null 2>&1 || fail "OpenSSL is required"
command -v python3 >/dev/null 2>&1 || fail "Python 3 is required"
init_compose_target
verify_compose_owner
mkdir -p secrets
chmod 700 secrets
[ ! -L "$COMMAND_LOCK" ] || fail "$COMMAND_LOCK must not be a symbolic link"
exec 7>"$COMMAND_LOCK" || fail "could not open the config-key rotation lock"
chmod 600 "$COMMAND_LOCK"
if ! _host_flock_nonblocking 7; then
  printf '%s\n' 'ERROR: config-key rotation is already in progress' >&2
  exit 1
fi

if [ -n "$(compose_for_install ps -q postgres-backup 2>/dev/null || true)" ]; then
  BACKUP_SERVICE_WAS_RUNNING=1
fi

phase="$(current_phase)"
if [ -s "$STATE_FILE" ]; then
  saved_backup_state="$(sed -n 's/^backup_service_was_running=//p' "$STATE_FILE" | head -1)"
  case "$saved_backup_state" in
    0|1) BACKUP_SERVICE_WAS_RUNNING="$saved_backup_state" ;;
  esac
  saved_guard_id="$(sed -n 's/^guard_id=//p' "$STATE_FILE" | head -1)"
  if [ -n "$saved_guard_id" ] \
     && ! printf '%s' "$saved_guard_id" | grep -Eq '^[0-9a-f]{32}$'; then
    fail "rotation state contains an invalid backup guard identity"
  fi
  ROTATION_GUARD_ID="$saved_guard_id"
  saved_cancel_restart="$(sed -n 's/^cancel_restart_services=//p' "$STATE_FILE" | head -1)"
  case "$saved_cancel_restart" in
    "") ;;
    0|1) CANCEL_RESTART_SERVICES="$saved_cancel_restart" ;;
    *) fail "rotation state contains an invalid cancellation recovery flag" ;;
  esac
fi
case "$phase" in
  new)
    cp -p .env "$ENV_BACKUP"
    chmod 600 "$ENV_BACKUP"
    cp "$ACTIVE_KEY" "$PREVIOUS_KEY"
    chmod 644 "$PREVIOUS_KEY"
    openssl rand -base64 32 | tr -d '\n' > "$NEXT_KEY"
    chmod 644 "$NEXT_KEY"
    write_state prepared
    phase=prepared
    ;;
  prepared|quiescing|cancelling|mutation-unknown|database-rotated|promoted|finalizing) ;;
  *) fail "unknown rotation state '$phase'; do not delete recovery files" ;;
esac

if [ "$phase" != finalizing ] && [ "$phase" != cancelling ]; then
  [ -s "$NEXT_KEY" ] || fail "$NEXT_KEY is missing; keep services stopped and restore from backup"
  [ -s "$PREVIOUS_KEY" ] || fail "$PREVIOUS_KEY is missing; keep services stopped and restore from backup"
  [ -s "$ENV_BACKUP" ] || fail "$ENV_BACKUP is missing; keep services stopped and restore from backup"
fi

if { [ "$phase" = prepared ] && [ -n "$ROTATION_GUARD_ID" ]; } \
   || [ "$phase" = quiescing ] \
   || { [ "$phase" = cancelling ] && [ -n "$ROTATION_GUARD_ID" ]; } \
   || [ "$phase" = mutation-unknown ] || [ "$phase" = database-rotated ] \
   || [ "$phase" = promoted ] || [ "$phase" = finalizing ]; then
  printf '%s\n' 'Re-entering the fail-closed backup maintenance window...'
  start_rotation_guard
fi

if [ "$phase" = quiescing ]; then
  printf '%s\n' 'Cancelling the interrupted pre-mutation rotation...'
  CANCEL_RESTART_SERVICES=1
  write_state cancelling
  phase=cancelling
fi

if [ "$phase" = cancelling ]; then
  finalize_cancellation
  exit 0
fi

if [ "$phase" = mutation-unknown ]; then
  compose_for_install stop paper_ingestion learning_engine postgres-backup
  reconcile_mutation_unknown
fi

if [ "$phase" = "prepared" ]; then
  printf '%s\n' 'Validating every encrypted Settings row with the current key...'
  if ! run_rotation_tool "$ACTIVE_KEY" "$NEXT_KEY"; then
    printf '%s\n' 'Current-key validation failed; checking whether a prior run already rotated the database.' >&2
    if run_rotation_tool "$NEXT_KEY" "$NEXT_KEY"; then
      write_state database-rotated
      phase=database-rotated
    else
      fail "neither the current nor staged key decrypts every encrypted row"
    fi
  fi
fi

if [ "$phase" = "prepared" ]; then
  if [ "$YES" -ne 1 ]; then
    if [ ! -t 0 ]; then
      fail "confirmation requires a terminal; re-run with --yes after taking a backup"
    fi
    printf '%s' 'A verified backup is required. Continue with a short maintenance window? [y/N] '
    read -r reply
    case "$reply" in
      y|Y|yes|YES|Yes) ;;
      *)
        CANCEL_RESTART_SERVICES=0
        write_state cancelling
        phase=cancelling
        finalize_cancellation
        exit 0
        ;;
    esac
  fi

  printf '%s\n' 'Waiting for any active backup to finish and blocking new backups...'
  start_rotation_guard
  CANCEL_RESTART_SERVICES=1
  write_state quiescing
  phase=quiescing
  printf '%s\n' 'Stopping services for the config-key maintenance window...'
  if [ "$BACKUP_SERVICE_WAS_RUNNING" -eq 1 ]; then
    compose_for_install stop paper_ingestion learning_engine postgres-backup
  else
    compose_for_install stop paper_ingestion learning_engine
  fi
  CANCEL_RESTART_SERVICES=0
  write_state mutation-unknown
  phase=mutation-unknown
  if ! run_rotation_tool "$ACTIVE_KEY" "$NEXT_KEY" --apply; then
    reconcile_mutation_unknown
  else
    write_state database-rotated
    phase=database-rotated
  fi
fi

if [ "$phase" = "database-rotated" ]; then
  write_env_key_from_file "$NEXT_KEY"
  cp "$NEXT_KEY" "${ACTIVE_KEY}.rotation"
  chmod 644 "${ACTIVE_KEY}.rotation"
  mv "${ACTIVE_KEY}.rotation" "$ACTIVE_KEY"
  write_state promoted
  phase=promoted
fi

if [ "$phase" = "promoted" ]; then
  printf '%s\n' 'Restarting services with the new key...'
  restart_services=(paper_ingestion learning_engine)
  [ "$BACKUP_SERVICE_WAS_RUNNING" -ne 1 ] || restart_services+=(postgres-backup)
  compose_for_install up -d --force-recreate "${restart_services[@]}"
  wait_service_healthy paper_ingestion || fail "paper_ingestion did not become healthy"
  wait_service_healthy learning_engine || fail "learning_engine did not become healthy"
  if [ "$BACKUP_SERVICE_WAS_RUNNING" -eq 1 ]; then
    wait_service_running postgres-backup || fail "postgres-backup did not restart"
  fi
  write_state finalizing
  phase=finalizing
fi

if [ "$phase" = finalizing ]; then
  # The phase record remains until every other staging artifact is gone. Each
  # removal is idempotent, so a crash at any point resumes here safely.
  rm -f -- "$NEXT_KEY" || fail "could not remove $NEXT_KEY; backups remain blocked"
  rm -f -- "$PREVIOUS_KEY" || fail "could not remove $PREVIOUS_KEY; backups remain blocked"
  rm -f -- "$ENV_BACKUP" || fail "could not remove $ENV_BACKUP; backups remain blocked"
  finish_rotation_guard clear || fail "could not clear the backup maintenance guard"
  rm -f -- "$STATE_FILE" || fail "could not remove $STATE_FILE; re-run rotation finalization"
  SUCCESS=1
  printf '%s\n' 'Config-key rotation complete. Both services are healthy.'
fi
