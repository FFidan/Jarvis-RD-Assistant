#!/usr/bin/env bash
# Operations that must run inside the postgres-backup service volume namespace.
# Host-side lifecycle commands invoke this script through an explicit, hardened
# `docker run`; they never depend on root-owned Docker volume paths.
set -euo pipefail

TRIGGER_DIR="${JARVIS_BACKUP_TRIGGER_DIR:-/backup-trigger}"
BACKUP_DIR="${JARVIS_BACKUP_DIR:-/backups}"
BACKUP_KEY_FILE="${JARVIS_BACKUP_KEY_FILE:-/run/secrets/backup_encrypt_key}"
LOCK_DIR="${BACKUP_DIR}/.lifecycle"
OPERATION_LOCK="${LOCK_DIR}/operation.lock"
OPERATION_STATE="${LOCK_DIR}/operation.state"
ADMISSION_LOCK="${LOCK_DIR}/operation-admission.lock"
UPDATE_LOCK="${LOCK_DIR}/update.lock"
UPDATE_GUARD="${LOCK_DIR}/update.guard"
UPDATE_CONTROL="${LOCK_DIR}/update.control"
UPDATE_RESERVATION="${LOCK_DIR}/update.reservation"
UPDATE_RESERVATION_LOCK="${LOCK_DIR}/update-reservation.lock"
ROTATION_LOCK="${LOCK_DIR}/backup.lock"
LEGACY_BACKUP_LOCK="${TRIGGER_DIR}/.backup.lock"
ROTATION_SENTINEL="${LOCK_DIR}/rotation.guard"
ROTATION_CONTROL="${LOCK_DIR}/rotation.control"
ROTATION_RESERVATION="${LOCK_DIR}/rotation.reservation"
ROTATION_RESERVATION_LOCK="${LOCK_DIR}/rotation-reservation.lock"
HOST_RESERVATION="${LOCK_DIR}/host.reservation"
HOST_CONTROL="${LOCK_DIR}/host.control"
HOST_RESERVATION_LOCK="${LOCK_DIR}/host-reservation.lock"
UPDATE_PIN="${LOCK_DIR}/update-backup-pin.json"
# Mirror of backup.sh's manifest domain label; all three scripts must agree byte-for-byte.
MANIFEST_HMAC_LABEL="jarvis-manifest-v1"

fail() { printf 'ERROR: %s\n' "$*" >&2; return 1; }

valid_id() { printf '%s' "$1" | grep -Eq '^[0-9a-f]{32}$'; }
valid_ts() { printf '%s' "$1" | grep -Eq '^[0-9]{8}_[0-9]{6}$'; }
valid_timeout() { printf '%s' "$1" | grep -Eq '^[1-9][0-9]{0,5}$'; }
valid_interval() {
  printf '%s' "$1" \
    | grep -Eq '^(0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*(\.[0-9]+)?)$'
}

prepare_lock_dir() {
  [ ! -L "$LOCK_DIR" ] || fail "backup lifecycle lock directory is a symbolic link"
  mkdir -p "$LOCK_DIR" || fail "could not create the backup lifecycle lock directory"
  [ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] \
    || fail "backup lifecycle lock directory is unsafe"
  chmod 700 "$LOCK_DIR" || fail "could not secure the backup lifecycle lock directory"
}

safe_private_lock_path() {
  local path="$1"
  [ ! -L "$path" ] || fail "backup lifecycle lock is a symbolic link: $path"
}

# Return 0 when fd 7 was free immediately, 2 when it was acquired only after a
# short status-probe collision, and 1 when a real owner persists. Holders never
# queue for an operation lifetime: a queued duplicate could otherwise wake
# after successful release and publish a new orphan guard.
claim_reservation_lock() {
  local path="$1" label="$2" attempt=0
  safe_private_lock_path "$path"
  exec 7>>"$path"
  flock -n 7 && return 0
  while [ "$attempt" -lt 25 ]; do
    sleep 0.01
    if flock -n 7; then
      return 2
    fi
    attempt=$((attempt + 1))
  done
  fail "${label} reservation is already owned"
}

# Every actor takes this lock inside the private postgres_backups named volume.
# Docker Desktop therefore keeps host-launched helpers and long-lived sidecars
# in one Linux flock domain even when the host itself is macOS. The adjacent
# state survives owner death; only the same operation identity may adopt it.
claim_operation_lock() {
  local operation="$1" kind="$2" id="$3" owner="" expected
  prepare_lock_dir
  safe_private_lock_path "$OPERATION_LOCK"
  safe_private_lock_path "$OPERATION_STATE"
  if [ ! -e "$OPERATION_LOCK" ]; then
    (set -C; umask 022; : > "$OPERATION_LOCK") 2>/dev/null || true
  fi
  [ -f "$OPERATION_LOCK" ] && [ ! -L "$OPERATION_LOCK" ] \
    || fail "lifecycle operation lock is unavailable or unsafe"
  chmod 600 "$OPERATION_LOCK" \
    || fail "could not secure the lifecycle operation lock"
  exec 5<>"$OPERATION_LOCK"
  if ! flock -n 5; then
    exec 5>&-
    fail "another lifecycle operation is active; wait for it to finish, then retry ${operation}"
  fi
  case "$kind" in
    update) expected="update-preparing:${id}" ;;
    rotation) expected="rotation:${id}" ;;
    setup|uninstall|control|direct-update) expected="${kind}:${id}" ;;
    *) exec 5>&-; fail "invalid lifecycle operation kind"; return ;;
  esac
  if [ -e "$OPERATION_STATE" ]; then
    [ -f "$OPERATION_STATE" ] && [ ! -L "$OPERATION_STATE" ] \
      || { exec 5>&-; fail "host lifecycle operation state is unsafe"; return; }
    owner="$(cat "$OPERATION_STATE" 2>/dev/null || true)"
    case "${kind}:${owner}" in
      "${kind}:${expected}"|"update:update:${id}") ;;
      *)
        exec 5>&-
        fail "another lifecycle operation has retained recovery state; finish that operation before retrying ${operation}"
        return
        ;;
    esac
  else
    atomic_write "$OPERATION_STATE" "$expected" \
      || { exec 5>&-; fail "could not persist lifecycle operation state"; return; }
  fi
}

claim_admission_lock() {
  prepare_lock_dir
  safe_private_lock_path "$ADMISSION_LOCK"
  exec 4>>"$ADMISSION_LOCK"
  flock 4 || fail "could not serialize lifecycle operation admission"
}

release_admission_lock() {
  flock -u 4 2>/dev/null || true
  exec 4>&-
}

operation_state_owner() {
  [ -f "$OPERATION_STATE" ] && [ ! -L "$OPERATION_STATE" ] || return 1
  cat "$OPERATION_STATE" 2>/dev/null
}

clear_operation_state() {
  local first="$1" second="${2:-}" owner
  owner="$(operation_state_owner 2>/dev/null || true)"
  if [ "$owner" = "$first" ] || { [ -n "$second" ] && [ "$owner" = "$second" ]; }; then
    rm -f "$OPERATION_STATE"
  fi
}

# v1.1.x backups used a trigger-volume mutex. New processes are serialized by
# the private lock above, but also take a safe, read-only lock on the old inode
# when it exists so an already-running pre-upgrade backup can finish. This
# compatibility read never creates, truncates, or follows an unsafe legacy path.
open_legacy_backup_lock() {
  local opened path_identity
  if [ ! -e "$LEGACY_BACKUP_LOCK" ] && [ ! -L "$LEGACY_BACKUP_LOCK" ]; then
    return 1
  fi
  if [ -L "$LEGACY_BACKUP_LOCK" ] || [ ! -f "$LEGACY_BACKUP_LOCK" ]; then
    printf 'WARN: unsafe legacy backup lock path: %s\n' "$LEGACY_BACKUP_LOCK" >&2
    return 2
  fi
  if ! exec 6<"$LEGACY_BACKUP_LOCK"; then
    printf 'WARN: could not inspect legacy backup lock: %s\n' "$LEGACY_BACKUP_LOCK" >&2
    return 2
  fi
  opened="$(stat -Lc '%d:%i' "/proc/$$/fd/6" 2>/dev/null || true)"
  path_identity="$(stat -Lc '%d:%i' "$LEGACY_BACKUP_LOCK" 2>/dev/null || true)"
  if [ -z "$opened" ] || [ "$opened" != "$path_identity" ] \
     || [ -L "$LEGACY_BACKUP_LOCK" ]; then
    exec 6>&-
    printf 'WARN: legacy backup lock changed while it was inspected\n' >&2
    return 2
  fi
  return 0
}

atomic_write() {
  local path="$1" body="$2" tmp
  tmp="$(mktemp "${path}.XXXXXX")" || return 1
  if ! printf '%s\n' "$body" > "$tmp" || ! chmod 600 "$tmp" || ! mv -f "$tmp" "$path"; then
    rm -f "$tmp"
    return 1
  fi
}

atomic_write_public() {
  local path="$1" body="$2" tmp
  tmp="$(mktemp "${path}.XXXXXX")" || return 1
  if ! printf '%s\n' "$body" > "$tmp" || ! chmod 644 "$tmp" || ! mv -f "$tmp" "$path"; then
    rm -f "$tmp"
    return 1
  fi
}

file_equals() {
  local path="$1" expected="$2"
  [ -f "$path" ] && [ ! -L "$path" ] \
    && [ "$(wc -l < "$path" 2>/dev/null || echo 0)" -eq 1 ] 2>/dev/null \
    && [ "$(cat "$path" 2>/dev/null || true)" = "$expected" ]
}

read_update_control() {
  local id="$1" value
  valid_id "$id" || return 2
  [ -f "$UPDATE_CONTROL" ] && [ ! -L "$UPDATE_CONTROL" ] || return 2
  [ "$(wc -l < "$UPDATE_CONTROL" 2>/dev/null || echo 0)" -eq 1 ] 2>/dev/null \
    || return 2
  value="$(cat "$UPDATE_CONTROL" 2>/dev/null || true)"
  case "$value" in
    "${id}:promote"|"${id}:yield-restore"|"${id}:release"|\
    "${id}:release:clear"|"${id}:release:retain")
      printf '%s' "$value"
      ;;
    *) return 2 ;;
  esac
}

valid_host_kind() {
  case "$1" in setup|uninstall|control|direct-update) return 0 ;; esac
  return 1
}

host_identity() {
  local kind="$1" id="$2"
  valid_host_kind "$kind" && valid_id "$id" || return 1
  printf '%s:%s' "$kind" "$id"
}

reserve_host() {
  local kind="$1" id="$2" expected owner=""
  expected="$(host_identity "$kind" "$id")" || fail "invalid host lifecycle identity"
  prepare_lock_dir
  claim_admission_lock
  owner="$(operation_state_owner 2>/dev/null || true)"
  if [ -e "$UPDATE_RESERVATION" ] || [ -L "$UPDATE_RESERVATION" ] \
      || [ -e "$ROTATION_RESERVATION" ] || [ -L "$ROTATION_RESERVATION" ] \
      || { [ -n "$owner" ] && [ "$owner" != "$expected" ]; }; then
    fail "another lifecycle operation is active; finish it before retrying ${kind}"
  fi
  safe_private_lock_path "$HOST_RESERVATION_LOCK"
  exec 7>>"$HOST_RESERVATION_LOCK"
  if flock -n 7; then
    if [ -e "$HOST_RESERVATION" ] || [ -L "$HOST_RESERVATION" ]; then
      file_equals "$HOST_RESERVATION" "$expected" \
        || fail "a different host lifecycle operation is already reserved"
    else
      atomic_write "$HOST_RESERVATION" "$expected"
    fi
    printf '%s\n' launch
    return 0
  fi
  file_equals "$HOST_RESERVATION" "$expected" \
    || fail "a different host lifecycle operation owns the reservation"
  printf '%s\n' adopt
}

host_reservation_owner_status() {
  local expected
  expected="$(host_identity "$1" "$2")" || return 1
  file_equals "$HOST_RESERVATION" "$expected" || return 1
  prepare_lock_dir
  safe_private_lock_path "$HOST_RESERVATION_LOCK"
  exec 7>>"$HOST_RESERVATION_LOCK"
  if flock -n 7; then
    flock -u 7
    exec 7>&-
    return 1
  fi
  exec 7>&-
  return 0
}

host_guard_status() {
  local expected
  expected="$(host_identity "$1" "$2")" || return 1
  file_equals "$OPERATION_STATE" "$expected" || return 1
  prepare_lock_dir
  safe_private_lock_path "$OPERATION_LOCK"
  [ -f "$OPERATION_LOCK" ] || return 1
  exec 8<>"$OPERATION_LOCK"
  if flock -n 8; then
    flock -u 8
    exec 8>&-
    return 1
  fi
  exec 8>&-
  return 0
}

current_host_operation() {
  local owner kind id
  owner="$(operation_state_owner 2>/dev/null || true)"
  kind="${owner%%:*}"
  id="${owner#*:}"
  [ "$owner" != "$id" ] && host_identity "$kind" "$id" >/dev/null \
    || return 1
  printf '%s\n' "$owner"
}

hold_host() {
  local kind="$1" id="$2" timeout="$3" expected deadline reservation_claim=0
  expected="$(host_identity "$kind" "$id")" || fail "invalid host lifecycle identity"
  valid_timeout "$timeout" || fail "invalid host lifecycle timeout"
  prepare_lock_dir
  claim_reservation_lock "$HOST_RESERVATION_LOCK" "host lifecycle" \
    || reservation_claim=$?
  case "$reservation_claim" in 0|2) ;; *) return "$reservation_claim" ;; esac
  file_equals "$HOST_RESERVATION" "$expected" \
    || fail "host lifecycle reservation does not match this helper"
  claim_operation_lock "$kind" "$kind" "$id"
  deadline=$(( $(date +%s) + timeout ))
  rm -f "$HOST_CONTROL"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if file_equals "$HOST_CONTROL" "${expected}:release:clear"; then
      rm -f "$HOST_CONTROL"
      file_equals "$HOST_RESERVATION" "$expected" \
        || fail "host lifecycle reservation changed identity"
      rm -f "$HOST_RESERVATION"
      clear_operation_state "$expected"
      return 0
    fi
    if file_equals "$HOST_CONTROL" "${expected}:release:retain"; then
      rm -f "$HOST_CONTROL"
      return 0
    fi
    sleep 0.1
  done
  fail "host lifecycle guard timed out; recovery state retained"
}

wait_host_activation() {
  local kind="$1" id="$2" startup_attempts="$3" interval="$4"
  local attempt=0 owner_misses=0 owner_seen=0
  host_identity "$kind" "$id" >/dev/null || fail "invalid host lifecycle identity"
  valid_timeout "$startup_attempts" || fail "invalid host lifecycle observer attempts"
  valid_interval "$interval" || fail "invalid host lifecycle observer interval"
  while [ "$attempt" -lt "$startup_attempts" ]; do
    host_guard_status "$kind" "$id" && return 0
    if host_reservation_owner_status "$kind" "$id"; then owner_seen=1; break; fi
    sleep "$interval"
    attempt=$((attempt + 1))
  done
  [ "$owner_seen" -eq 1 ] \
    || { host_guard_status "$kind" "$id" && return 0; fail "host reservation owner was not observed before guard activation"; return; }
  while true; do
    host_guard_status "$kind" "$id" && return 0
    if host_reservation_owner_status "$kind" "$id"; then
      owner_misses=0
    else
      owner_misses=$((owner_misses + 1))
      if [ "$owner_misses" -ge 2 ]; then
        host_guard_status "$kind" "$id" && return 0
        fail "host reservation owner stopped before guard activation"
        return
      fi
    fi
    sleep "$interval"
  done
}

release_host() {
  local kind="$1" id="$2" action="$3" expected
  case "$action" in clear|retain) ;; *) fail "invalid host lifecycle release action"; return ;; esac
  expected="$(host_identity "$kind" "$id")" || fail "invalid host lifecycle identity"
  host_guard_status "$kind" "$id" || fail "host lifecycle guard is not active"
  atomic_write "$HOST_CONTROL" "${expected}:release:${action}"
}

host_release_complete() {
  local kind="$1" id="$2" action="$3" expected
  expected="$(host_identity "$kind" "$id")" || return 1
  host_guard_status "$kind" "$id" && return 1
  [ ! -e "$HOST_CONTROL" ] && [ ! -L "$HOST_CONTROL" ] || return 1
  case "$action" in
    clear)
      [ ! -e "$HOST_RESERVATION" ] && [ ! -L "$HOST_RESERVATION" ] \
        && [ "$(operation_state_owner 2>/dev/null || true)" != "$expected" ]
      ;;
    retain)
      file_equals "$HOST_RESERVATION" "$expected" \
        && file_equals "$OPERATION_STATE" "$expected"
      ;;
    *) return 1 ;;
  esac
}

cancel_host_reservation() {
  local kind="$1" id="$2" expected owner=""
  expected="$(host_identity "$kind" "$id")" || fail "invalid host lifecycle identity"
  prepare_lock_dir
  claim_admission_lock
  safe_private_lock_path "$HOST_RESERVATION_LOCK"
  exec 7>>"$HOST_RESERVATION_LOCK"
  flock -n 7 || fail "host lifecycle reservation owner is still active"
  file_equals "$HOST_RESERVATION" "$expected" \
    || fail "host lifecycle reservation identity changed"
  owner="$(operation_state_owner 2>/dev/null || true)"
  [ -z "$owner" ] || fail "host lifecycle operation activated; retry it for recovery"
  rm -f "$HOST_RESERVATION"
}

clear_retained_host() {
  local kind="$1" id="$2" expected
  expected="$(host_identity "$kind" "$id")" || fail "invalid host lifecycle identity"
  prepare_lock_dir
  claim_admission_lock
  safe_private_lock_path "$OPERATION_LOCK"
  safe_private_lock_path "$HOST_RESERVATION_LOCK"
  exec 5<>"$OPERATION_LOCK"
  flock -n 5 || fail "host lifecycle guard is still active"
  exec 7>>"$HOST_RESERVATION_LOCK"
  flock -n 7 || fail "host lifecycle reservation owner is still active"
  file_equals "$OPERATION_STATE" "$expected" \
    && file_equals "$HOST_RESERVATION" "$expected" \
    || fail "retained host lifecycle identity changed"
  rm -f "$HOST_RESERVATION" "$HOST_CONTROL" "$OPERATION_STATE"
}

reserve_update() {
  local id="$1" owner=""
  valid_id "$id" || fail "invalid update guard id"
  mkdir -p "$TRIGGER_DIR"
  prepare_lock_dir
  claim_admission_lock
  owner="$(operation_state_owner 2>/dev/null || true)"
  if [ -e "$HOST_RESERVATION" ] || [ -L "$HOST_RESERVATION" ] \
      || [ -e "$ROTATION_SENTINEL" ] || [ -L "$ROTATION_SENTINEL" ] \
      || [ -e "$ROTATION_RESERVATION" ] || [ -L "$ROTATION_RESERVATION" ] \
      || { [ -n "$owner" ] \
           && [ "$owner" != "update-preparing:${id}" ] \
           && [ "$owner" != "update:${id}" ]; }; then
    fail "another lifecycle operation is active; finish it before retrying the update"
  fi
  safe_private_lock_path "$UPDATE_RESERVATION_LOCK"
  exec 7>>"$UPDATE_RESERVATION_LOCK"
  if flock -n 7; then
    if [ -e "$UPDATE_RESERVATION" ] || [ -L "$UPDATE_RESERVATION" ]; then
      file_equals "$UPDATE_RESERVATION" "$id" \
        || fail "a different update lifecycle guard is already reserved"
    else
      atomic_write "$UPDATE_RESERVATION" "$id"
    fi
    printf '%s\n' launch
    return 0
  fi
  file_equals "$UPDATE_RESERVATION" "$id" \
    || fail "a different update lifecycle guard owns the reservation"
  printf '%s\n' adopt
}

update_reservation_owner_status() {
  local id="$1"
  valid_id "$id" || return 1
  file_equals "$UPDATE_RESERVATION" "$id" || return 1
  prepare_lock_dir
  safe_private_lock_path "$UPDATE_RESERVATION_LOCK"
  exec 7>>"$UPDATE_RESERVATION_LOCK"
  if flock -n 7; then
    flock -u 7
    exec 7>&-
    return 1
  fi
  exec 7>&-
  return 0
}

current_update_reservation() {
  local id
  [ -f "$UPDATE_RESERVATION" ] && [ ! -L "$UPDATE_RESERVATION" ] || return 1
  id="$(cat "$UPDATE_RESERVATION" 2>/dev/null || true)"
  valid_id "$id" || return 1
  file_equals "$UPDATE_RESERVATION" "$id" || return 1
  printf '%s\n' "$id"
}

hold_update() {
  local id="$1" timeout="$2" deadline reservation_claim=0 control owner
  valid_id "$id" || fail "invalid update guard id"
  valid_timeout "$timeout" || fail "invalid update guard timeout"
  mkdir -p "$TRIGGER_DIR"
  prepare_lock_dir
  safe_private_lock_path "$UPDATE_LOCK"
  claim_reservation_lock "$UPDATE_RESERVATION_LOCK" "update lifecycle" \
    || reservation_claim=$?
  case "$reservation_claim" in 0|2) ;; *) return "$reservation_claim" ;; esac
  file_equals "$UPDATE_RESERVATION" "$id" \
    || fail "update lifecycle reservation does not match this helper"
  if [ "$reservation_claim" -eq 2 ] \
     && { [ -e "$UPDATE_GUARD" ] || [ -L "$UPDATE_GUARD" ]; }; then
    fail "update lifecycle guard already activated before this helper acquired ownership"
  fi
  claim_operation_lock "the update" update "$id"
  exec 9>>"$UPDATE_LOCK"
  flock -w "$timeout" 9 || fail "timed out waiting for the update lifecycle mutex"
  deadline=$(( $(date +%s) + timeout ))
  rm -f "$UPDATE_CONTROL"
  atomic_write "$UPDATE_GUARD" "$id"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ -e "$UPDATE_CONTROL" ] || [ -L "$UPDATE_CONTROL" ]; then
      # The command writer releases admission before waiting. Re-read and apply
      # the state transition under that same lock so cancellation cannot land
      # between the control read and the durable transition.
      claim_admission_lock
      control="$(read_update_control "$id")" \
        || fail "update lifecycle control is malformed or belongs to another operation"
      owner="$(operation_state_owner 2>/dev/null || true)"
      case "$control" in
        "${id}:promote")
          [ "$owner" = "update-preparing:${id}" ] \
            || fail "update lifecycle promotion no longer matches the preparing state"
          atomic_write "$OPERATION_STATE" "update:${id}" \
            || fail "could not promote the update lifecycle state"
          rm -f "$UPDATE_CONTROL"
          release_admission_lock
          continue
          ;;
        "${id}:yield-restore")
          [ "$owner" = "update-preparing:${id}" ] \
            || fail "restore yield no longer matches the preparing update state"
          file_equals "$UPDATE_GUARD" "$id" \
            || fail "update lifecycle guard changed identity"
          file_equals "$UPDATE_RESERVATION" "$id" \
            || fail "update lifecycle reservation changed identity"
          rm -f "$UPDATE_CONTROL" "$UPDATE_GUARD" "$UPDATE_RESERVATION"
          clear_operation_state "update-preparing:${id}"
          release_admission_lock
          return 0
          ;;
        "${id}:release"|"${id}:release:clear")
          file_equals "$UPDATE_GUARD" "$id" \
            || fail "update lifecycle guard changed identity"
          file_equals "$UPDATE_RESERVATION" "$id" \
            || fail "update lifecycle reservation changed identity"
          rm -f "$UPDATE_CONTROL" "$UPDATE_GUARD" "$UPDATE_RESERVATION"
          clear_operation_state "update-preparing:${id}" "update:${id}"
          release_admission_lock
          return 0
          ;;
        "${id}:release:retain")
          file_equals "$UPDATE_GUARD" "$id" \
            || fail "update lifecycle guard changed identity"
          file_equals "$UPDATE_RESERVATION" "$id" \
            || fail "update lifecycle reservation changed identity"
          rm -f "$UPDATE_CONTROL" "$UPDATE_GUARD"
          release_admission_lock
          return 0
          ;;
      esac
    fi
    sleep 0.1
  done
  # Serialize deadline cleanup with a last command publication. Promotion is
  # cancelled after the holder deadline; yield and release remain safe cleanup
  # commands and are completed rather than left as orphan control state.
  claim_admission_lock
  if [ -e "$UPDATE_CONTROL" ] || [ -L "$UPDATE_CONTROL" ]; then
    control="$(read_update_control "$id")" \
      || fail "update lifecycle control is malformed or belongs to another operation"
    owner="$(operation_state_owner 2>/dev/null || true)"
    case "$control" in
      "${id}:promote")
        rm -f "$UPDATE_CONTROL"
        ;;
      "${id}:yield-restore")
        [ "$owner" = "update-preparing:${id}" ] \
          || fail "restore yield no longer matches the preparing update state"
        file_equals "$UPDATE_GUARD" "$id" \
          && file_equals "$UPDATE_RESERVATION" "$id" \
          || fail "update lifecycle identity changed before restore yield"
        rm -f "$UPDATE_CONTROL" "$UPDATE_GUARD" "$UPDATE_RESERVATION"
        clear_operation_state "update-preparing:${id}"
        release_admission_lock
        return 0
        ;;
      "${id}:release"|"${id}:release:clear")
        file_equals "$UPDATE_GUARD" "$id" \
          && file_equals "$UPDATE_RESERVATION" "$id" \
          || fail "update lifecycle identity changed before release"
        rm -f "$UPDATE_CONTROL" "$UPDATE_GUARD" "$UPDATE_RESERVATION"
        clear_operation_state "update-preparing:${id}" "update:${id}"
        release_admission_lock
        return 0
        ;;
      "${id}:release:retain")
        file_equals "$UPDATE_GUARD" "$id" \
          && file_equals "$UPDATE_RESERVATION" "$id" \
          || fail "update lifecycle identity changed before release"
        rm -f "$UPDATE_CONTROL" "$UPDATE_GUARD"
        release_admission_lock
        return 0
        ;;
    esac
  fi
  owner="$(operation_state_owner 2>/dev/null || true)"
  file_equals "$UPDATE_GUARD" "$id" \
    && file_equals "$UPDATE_RESERVATION" "$id" \
    || fail "update lifecycle identity changed before timeout"
  case "$owner" in
    "update-preparing:${id}")
      rm -f "$UPDATE_GUARD" "$UPDATE_RESERVATION"
      clear_operation_state "update-preparing:${id}"
      ;;
    "update:${id}")
      rm -f "$UPDATE_GUARD"
      ;;
    *) fail "update lifecycle state changed identity before timeout" ;;
  esac
  release_admission_lock
  fail "update lifecycle guard timed out"
}

wait_update_activation() {
  local id="$1" startup_attempts="$2" interval="$3"
  local attempt=0 owner_misses=0 owner_seen=0
  valid_id "$id" || fail "invalid update guard id"
  valid_timeout "$startup_attempts" || fail "invalid update observer startup attempts"
  valid_interval "$interval" || fail "invalid update observer interval"
  while [ "$attempt" -lt "$startup_attempts" ]; do
    update_guard_status "$id" && return 0
    if update_reservation_owner_status "$id"; then
      owner_seen=1
      break
    fi
    sleep "$interval"
    attempt=$((attempt + 1))
  done
  if [ "$owner_seen" -ne 1 ]; then
    update_guard_status "$id" && return 0
    fail "update reservation owner was not observed before guard activation"
    return 1
  fi
  while true; do
    update_guard_status "$id" && return 0
    if update_reservation_owner_status "$id"; then
      owner_misses=0
    else
      owner_misses=$((owner_misses + 1))
      if [ "$owner_misses" -ge 2 ]; then
        update_guard_status "$id" && return 0
        fail "update reservation owner stopped before guard activation"
        return 1
      fi
    fi
    sleep "$interval"
  done
}

update_guard_status() {
  local id="$1"
  valid_id "$id" || return 1
  file_equals "$UPDATE_GUARD" "$id" || return 1
  prepare_lock_dir
  safe_private_lock_path "$UPDATE_LOCK"
  exec 8>>"$UPDATE_LOCK"
  if flock -n 8; then
    flock -u 8
    exec 8>&-
    return 1
  fi
  exec 8>&-
  return 0
}

promoted_update_status() {
  local id="$1"
  update_guard_status "$id" || return 1
  file_equals "$OPERATION_STATE" "update:${id}"
}

current_update_guard() {
  local id
  [ -f "$UPDATE_GUARD" ] && [ ! -L "$UPDATE_GUARD" ] || return 1
  id="$(cat "$UPDATE_GUARD" 2>/dev/null || true)"
  update_guard_status "$id" || return 1
  printf '%s\n' "$id"
}

release_update() {
  local id="$1" action="${2:-clear}" attempt=0 control
  valid_id "$id" || fail "invalid update guard id"
  case "$action" in clear|retain) ;; *) fail "invalid update release action"; return ;; esac
  while [ "$attempt" -lt 100 ]; do
    prepare_lock_dir
    claim_admission_lock
    if ! update_guard_status "$id"; then
      if [ ! -e "$UPDATE_GUARD" ] && [ ! -L "$UPDATE_GUARD" ] \
          && [ ! -e "$UPDATE_RESERVATION" ] && [ ! -L "$UPDATE_RESERVATION" ] \
          && [ ! -e "$UPDATE_CONTROL" ] && [ ! -L "$UPDATE_CONTROL" ] \
          && [ ! -e "$OPERATION_STATE" ] && [ ! -L "$OPERATION_STATE" ]; then
        release_admission_lock
        return 0
      fi
      release_admission_lock
      fail "update lifecycle guard is not active"
      return
    fi
    control=""
    if [ -e "$UPDATE_CONTROL" ] || [ -L "$UPDATE_CONTROL" ]; then
      control="$(read_update_control "$id")" \
        || { release_admission_lock; fail "update lifecycle control is malformed or belongs to another operation"; return; }
    fi
    case "$control" in
      "")
        atomic_write "$UPDATE_CONTROL" "${id}:release:${action}" \
          || { release_admission_lock; fail "could not persist the update release request"; return; }
        release_admission_lock
        return 0
        ;;
      "${id}:yield-restore")
        release_admission_lock
        return 0
        ;;
      "${id}:promote")
        release_admission_lock
        sleep 0.05
        attempt=$((attempt + 1))
        ;;
      "${id}:release"|"${id}:release:clear")
        release_admission_lock
        [ "$action" = clear ] \
          || fail "a different update release request is already pending"
        return
        ;;
      "${id}:release:retain")
        release_admission_lock
        [ "$action" = retain ] \
          || fail "a different update release request is already pending"
        return
        ;;
    esac
  done
  fail "timed out waiting for update promotion before release"
}

promote_update() {
  local id="$1" attempt=0 owner control=""
  local wait_error="timed out promoting the update lifecycle state"
  valid_id "$id" || fail "invalid update guard id"
  prepare_lock_dir
  claim_admission_lock
  update_guard_status "$id" \
    || { release_admission_lock; fail "update lifecycle guard is not active"; return; }
  owner="$(operation_state_owner 2>/dev/null || true)"
  if [ "$owner" = "update:${id}" ]; then
    if [ -e "$UPDATE_CONTROL" ] || [ -L "$UPDATE_CONTROL" ]; then
      release_admission_lock
      fail "promoted update has incomplete or unsafe control state"
      return
    fi
    release_admission_lock
    return 0
  fi
  if [ "$owner" != "update-preparing:${id}" ]; then
    release_admission_lock
    fail "update lifecycle state is not promotable"
    return
  fi
  if [ -e "$UPDATE_CONTROL" ] || [ -L "$UPDATE_CONTROL" ]; then
    control="$(read_update_control "$id")" \
      || { release_admission_lock; fail "update lifecycle control is malformed or belongs to another operation"; return; }
  fi
  case "$control" in
    "")
      atomic_write "$UPDATE_CONTROL" "${id}:promote" \
        || { release_admission_lock; fail "could not persist update promotion"; return; }
      ;;
    "${id}:promote") ;;
    "${id}:yield-restore")
      release_admission_lock
      fail "restore already won admission against the preparing update"
      return
      ;;
    *)
      release_admission_lock
      fail "update lifecycle is already being released"
      return
      ;;
  esac
  release_admission_lock
  while [ "$attempt" -lt 100 ]; do
    owner="$(operation_state_owner 2>/dev/null || true)"
    if [ "$owner" = "update:${id}" ]; then
      wait_error="update lifecycle guard stopped before mutation admission"
      break
    fi
    if ! update_guard_status "$id"; then
      wait_error="update lifecycle guard stopped before mutation admission"
      break
    fi
    sleep 0.05
    attempt=$((attempt + 1))
  done
  # A timed-out requester may cancel only its still-queued exact command. If the
  # holder linearized first, the promoted durable state makes this a success.
  prepare_lock_dir
  claim_admission_lock
  owner="$(operation_state_owner 2>/dev/null || true)"
  control=""
  if [ -e "$UPDATE_CONTROL" ] || [ -L "$UPDATE_CONTROL" ]; then
    control="$(read_update_control "$id")" \
      || { release_admission_lock; fail "update lifecycle control is malformed or belongs to another operation"; return; }
  fi
  if [ "$owner" = "update:${id}" ] \
      && update_guard_status "$id" \
      && [ -z "$control" ]; then
    release_admission_lock
    return 0
  fi
  if [ "$control" = "${id}:promote" ]; then
    rm -f "$UPDATE_CONTROL"
  fi
  release_admission_lock
  fail "$wait_error"
}

reserve_rotation() {
  local id="$1" owner=""
  valid_id "$id" || fail "invalid rotation guard id"
  mkdir -p "$TRIGGER_DIR"
  prepare_lock_dir
  claim_admission_lock
  owner="$(operation_state_owner 2>/dev/null || true)"
  if [ -e "$HOST_RESERVATION" ] || [ -L "$HOST_RESERVATION" ] \
      || [ -e "$UPDATE_GUARD" ] || [ -L "$UPDATE_GUARD" ] \
      || [ -e "$UPDATE_RESERVATION" ] || [ -L "$UPDATE_RESERVATION" ] \
      || { [ -n "$owner" ] && [ "$owner" != "rotation:${id}" ]; }; then
    fail "another lifecycle operation is active; finish it before retrying config-key rotation"
  fi
  safe_private_lock_path "$ROTATION_RESERVATION_LOCK"
  exec 7>>"$ROTATION_RESERVATION_LOCK"
  if flock -n 7; then
    if [ -e "$ROTATION_RESERVATION" ] || [ -L "$ROTATION_RESERVATION" ]; then
      file_equals "$ROTATION_RESERVATION" "$id" \
        || fail "a different config-key rotation is already reserved"
    else
      atomic_write "$ROTATION_RESERVATION" "$id"
    fi
    printf '%s\n' launch
    return 0
  fi
  file_equals "$ROTATION_RESERVATION" "$id" \
    || fail "a different config-key rotation owns the reservation"
  printf '%s\n' adopt
}

rotation_reservation_owner_status() {
  local id="$1"
  valid_id "$id" || return 1
  file_equals "$ROTATION_RESERVATION" "$id" || return 1
  prepare_lock_dir
  safe_private_lock_path "$ROTATION_RESERVATION_LOCK"
  exec 7>>"$ROTATION_RESERVATION_LOCK"
  if flock -n 7; then
    flock -u 7
    exec 7>&-
    return 1
  fi
  exec 7>&-
  return 0
}

hold_rotation() {
  local id="$1" timeout="$2" deadline remaining legacy_state=0 reservation_claim=0
  valid_id "$id" || fail "invalid rotation guard id"
  valid_timeout "$timeout" || fail "invalid rotation guard timeout"
  mkdir -p "$TRIGGER_DIR"
  prepare_lock_dir
  safe_private_lock_path "$ROTATION_LOCK"
  claim_reservation_lock "$ROTATION_RESERVATION_LOCK" "config-key rotation" \
    || reservation_claim=$?
  case "$reservation_claim" in 0|2) ;; *) return "$reservation_claim" ;; esac
  file_equals "$ROTATION_RESERVATION" "$id" \
    || fail "config-key rotation reservation does not match this helper"
  if [ "$reservation_claim" -eq 2 ] \
     && { [ -e "$ROTATION_SENTINEL" ] || [ -L "$ROTATION_SENTINEL" ]; }; then
    fail "config-key rotation already activated before this helper acquired ownership"
  fi
  claim_operation_lock "config-key rotation" rotation "$id"
  deadline=$(( $(date +%s) + timeout ))
  exec 9>>"$ROTATION_LOCK"
  flock -w "$timeout" 9 || fail "timed out waiting for the backup maintenance mutex"
  open_legacy_backup_lock || legacy_state=$?
  [ "$legacy_state" -ne 2 ] \
    || fail "unsafe legacy backup lock; confirm no pre-upgrade backup is running, remove the trigger-volume lock path, and retry"
  if [ "$legacy_state" -eq 0 ]; then
    remaining=$(( deadline - $(date +%s) ))
    [ "$remaining" -gt 0 ] \
      && flock -w "$remaining" 6 \
      || fail "timed out waiting for a pre-upgrade backup to finish"
  fi
  deadline=$(( $(date +%s) + timeout ))
  rm -f "$ROTATION_CONTROL"
  atomic_write "$ROTATION_SENTINEL" "$id"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if file_equals "$ROTATION_CONTROL" "${id}:clear"; then
      rm -f "$ROTATION_CONTROL"
      file_equals "$ROTATION_SENTINEL" "$id" \
        || fail "config-key rotation sentinel changed identity"
      file_equals "$ROTATION_RESERVATION" "$id" \
        || fail "config-key rotation reservation changed identity"
      rm -f "$ROTATION_SENTINEL" "$ROTATION_RESERVATION"
      clear_operation_state "rotation:${id}"
      return 0
    fi
    if file_equals "$ROTATION_CONTROL" "${id}:retain"; then
      rm -f "$ROTATION_CONTROL"
      return 0
    fi
    sleep 0.1
  done
  # A timed-out or killed rotation remains fail-closed: backup.sh treats the
  # durable sentinel as active regardless of age.
  fail "config-key rotation guard timed out; maintenance sentinel retained"
}

rotation_guard_status() {
  local id="$1"
  valid_id "$id" || return 1
  file_equals "$ROTATION_SENTINEL" "$id" || return 1
  prepare_lock_dir
  safe_private_lock_path "$ROTATION_LOCK"
  exec 8>>"$ROTATION_LOCK"
  if flock -n 8; then
    flock -u 8
    exec 8>&-
    return 1
  fi
  exec 8>&-
  return 0
}

# Wait inside one short-lived sidecar container, not by launching a new Compose
# container for every poll. The reservation owner has its own bounded lock wait;
# this observer returns failure only after that owner is gone twice and a final
# guard check proves it cannot publish a sentinel later.
wait_rotation_activation() {
  local id="$1" startup_attempts="$2" interval="$3"
  local attempt=0 owner_misses=0 owner_seen=0
  valid_id "$id" || fail "invalid rotation guard id"
  valid_timeout "$startup_attempts" || fail "invalid rotation observer startup attempts"
  valid_interval "$interval" || fail "invalid rotation observer interval"
  while [ "$attempt" -lt "$startup_attempts" ]; do
    rotation_guard_status "$id" && return 0
    if rotation_reservation_owner_status "$id"; then
      owner_seen=1
      break
    fi
    sleep "$interval"
    attempt=$((attempt + 1))
  done
  if [ "$owner_seen" -ne 1 ]; then
    rotation_guard_status "$id" && return 0
    fail "rotation reservation owner was not observed before guard activation"
    return 1
  fi
  while true; do
    if rotation_guard_status "$id"; then
      return 0
    fi
    if rotation_reservation_owner_status "$id"; then
      owner_misses=0
    else
      owner_misses=$((owner_misses + 1))
      if [ "$owner_misses" -ge 2 ]; then
        rotation_guard_status "$id" && return 0
        fail "rotation reservation owner stopped before guard activation"
        return 1
      fi
    fi
    sleep "$interval"
  done
}

current_rotation_guard() {
  local id
  [ -f "$ROTATION_SENTINEL" ] && [ ! -L "$ROTATION_SENTINEL" ] || return 1
  id="$(cat "$ROTATION_SENTINEL" 2>/dev/null || true)"
  rotation_guard_status "$id" || return 1
  printf '%s\n' "$id"
}

release_rotation() {
  local id="$1" action="$2"
  case "$action" in clear|retain) ;; *) fail "invalid rotation release action" ;; esac
  rotation_guard_status "$id" || fail "config-key rotation guard is not active"
  atomic_write "$ROTATION_CONTROL" "${id}:${action}"
}

rotation_release_complete() {
  local id="$1" action="$2"
  valid_id "$id" || return 1
  [ ! -e "$ROTATION_CONTROL" ] && [ ! -L "$ROTATION_CONTROL" ] || return 1
  case "$action" in
    clear)
      [ ! -e "$ROTATION_SENTINEL" ] && [ ! -L "$ROTATION_SENTINEL" ] \
        && [ ! -e "$ROTATION_RESERVATION" ] && [ ! -L "$ROTATION_RESERVATION" ]
      ;;
    retain)
      file_equals "$ROTATION_SENTINEL" "$id" \
        && file_equals "$ROTATION_RESERVATION" "$id"
      ;;
    *) return 1 ;;
  esac
}

publish_request() {
  local request_id="$1"
  valid_id "$request_id" || fail "invalid backup request id"
  mkdir -p "$TRIGGER_DIR"
  atomic_write "${TRIGGER_DIR}/.backup_now" "$request_id"
}

pin_body() {
  local ts="$1" run_id="$2" legacy="$3"
  valid_ts "$ts" || return 1
  if [ "$legacy" = true ]; then
    [ -z "$run_id" ] || return 1
    printf '{"timestamp":"%s","run_id":null,"legacy_recovery":true}' "$ts"
  else
    valid_id "$run_id" || return 1
    printf '{"timestamp":"%s","run_id":"%s"}' "$ts" "$run_id"
  fi
}

write_pin() {
  local body
  body="$(pin_body "$1" "$2" "$3")" || fail "invalid update backup pin"
  if [ -e "$UPDATE_PIN" ] || [ -L "$UPDATE_PIN" ]; then
    file_equals "$UPDATE_PIN" "$body" || fail "a different update backup pin already exists"
    return 0
  fi
  atomic_write "$UPDATE_PIN" "$body"
}

pin_matches() {
  local body
  body="$(pin_body "$1" "$2" "$3")" || return 1
  file_equals "$UPDATE_PIN" "$body"
}

clear_pin() {
  [ -e "$UPDATE_PIN" ] || [ -L "$UPDATE_PIN" ] || return 0
  pin_matches "$1" "$2" "$3" || fail "update backup pin does not match"
  rm -f "$UPDATE_PIN"
}

# Mirrors backup.sh's signing construction: the PUBLIC label keys the HMAC and the
# SECRET key-file bytes are the message prefix, so the key never reaches argv.
manifest_signature() {
  { cat -- "$BACKUP_KEY_FILE"; printf '\n%s\n' "$MANIFEST_HMAC_LABEL"; cat -- "$1"; } \
    | openssl dgst -sha256 -hmac "$MANIFEST_HMAC_LABEL" -r 2>/dev/null | cut -d' ' -f1
}

# The construction releases before 1.2.6 wrote, kept for verification only so a backup
# set taken by an older release still authenticates. Nothing signs this way any more.
# Computed in perl because openssl can only be keyed on the derived secret through argv,
# where any account on the host can read it; Digest::SHA takes it as an argument to a
# function instead. Both are HMAC-SHA256 and agree byte for byte.
legacy_manifest_signature() {
  perl -MDigest::SHA=hmac_sha256_hex -e '
    use strict; use warnings;
    my ($label, $keyfile, $manifest) = @ARGV;
    local $/;
    open(my $k, "<:raw", $keyfile) or exit 1; my $key = <$k>;
    open(my $m, "<:raw", $manifest) or exit 1; my $msg = <$m>;
    defined $key && defined $msg or exit 1;
    print hmac_sha256_hex($msg, pack("H*", hmac_sha256_hex($key, $label))), "\n";
  ' "$MANIFEST_HMAC_LABEL" "$BACKUP_KEY_FILE" "$1" 2>/dev/null
}

verify_manifest_hmac() {
  local manifest="$1" stored computed
  [ -f "$BACKUP_KEY_FILE" ] && [ ! -L "$BACKUP_KEY_FILE" ] \
    || { fail "backup authentication key is missing or unsafe"; return 1; }
  [ -f "${manifest}.hmac" ] && [ ! -L "$manifest" ] && [ ! -L "${manifest}.hmac" ] \
    || { fail "backup manifest has no safe HMAC signature"; return 1; }
  stored="$(cat "${manifest}.hmac" 2>/dev/null || true)"
  printf '%s' "$stored" | grep -Eq '^[0-9a-f]{64}$' \
    || { fail "backup manifest signature is malformed"; return 1; }
  computed="$(manifest_signature "$manifest")"
  printf '%s' "$computed" | grep -Eq '^[0-9a-f]{64}$' \
    || { fail "could not compute the backup manifest signature"; return 1; }
  if [ "$stored" != "$computed" ]; then
    computed="$(legacy_manifest_signature "$manifest" || true)"
    printf '%s' "$computed" | grep -Eq '^[0-9a-f]{64}$' \
      || { fail "backup manifest failed authentication"; return 1; }
    [ "$stored" = "$computed" ] || { fail "backup manifest failed authentication"; return 1; }
  fi
}

verify_backup_set() {
  local manifest="$1" expected_run_id="$2" shape="$3" state archive_re entries
  local manifest_base manifest_ts signed_ts signed_run_id e fn sha size actual_sha actual_size
  local jarvis_seen=0 litellm_seen=0 pdfs_seen=0 secrets_seen=0 role collection
  local seen_files="" seen_roles="" disk base
  manifest_base="$(basename "$manifest")"
  manifest_ts="$(printf '%s' "$manifest_base" | sed -nE 's/^manifest_([0-9]{8}_[0-9]{6})\.json$/\1/p')"
  [ -n "$manifest_ts" ] || { fail "backup manifest name is invalid"; return 1; }
  if [ "$shape" = current ]; then
    valid_id "$expected_run_id" || { fail "backup request id is invalid"; return 1; }
  elif [ "$shape" != legacy ] || [ -n "$expected_run_id" ]; then
    fail "backup manifest compatibility mode is invalid"
    return 1
  fi
  verify_manifest_hmac "$manifest" || return 1

  state="$(cat "$manifest" 2>/dev/null || true)"
  [ "$(wc -l < "$manifest" 2>/dev/null || echo 1)" -eq 0 ] 2>/dev/null \
    || { fail "backup manifest is multi-line or malformed"; return 1; }
  [ "${#state}" -le 1048576 ] || { fail "backup manifest is unreasonably large"; return 1; }
  archive_re='\{"filename":"(jarvis_[0-9]{8}_[0-9]{6}\.sql\.gz\.enc|litellm_[0-9]{8}_[0-9]{6}\.sql\.gz\.enc|pdfs_[0-9]{8}_[0-9]{6}\.tar\.gz\.enc|secrets_[0-9]{8}_[0-9]{6}\.tar\.gz\.enc|qdrant_[A-Za-z0-9_-]+_[0-9]{8}_[0-9]{6}\.snapshot\.enc)","sha256":"[0-9a-f]{64}","size_bytes":[1-9][0-9]*\}'
  if [ "$shape" = legacy ]; then
    printf '%s' "$state" | grep -Eq "^\\{\"timestamp\":\"[0-9]{8}_[0-9]{6}\",\"app_version\":\"(unknown|[A-Za-z0-9][A-Za-z0-9._+-]*)\",\"schema_version\":[0-9]+,\"created_at\":\"[0-9T:+.-]+\",\"archives\":\\[${archive_re}(,${archive_re})*\\]\\}$" \
      || { fail "legacy backup manifest has an unknown or incomplete schema"; return 1; }
  else
    printf '%s' "$state" | grep -Eq "^\\{\"timestamp\":\"[0-9]{8}_[0-9]{6}\",\"run_id\":\"[0-9a-f]{32}\",\"app_version\":\"(unknown|[A-Za-z0-9][A-Za-z0-9._+-]*)\",\"schema_version\":[0-9]+,\"created_at\":\"[0-9T:+.-]+\",\"archives\":\\[${archive_re}(,${archive_re})*\\]\\}$" \
      || { fail "backup manifest has an unknown or incomplete schema"; return 1; }
  fi
  signed_ts="$(printf '%s' "$state" | grep -oE '"timestamp":"[0-9]{8}_[0-9]{6}"' | cut -d'"' -f4)"
  signed_run_id="$(printf '%s' "$state" | grep -oE '"run_id":"[0-9a-f]{32}"' | cut -d'"' -f4 || true)"
  if [ "$signed_ts" != "$manifest_ts" ] \
     || { [ "$shape" = current ] && [ "$signed_run_id" != "$expected_run_id" ]; }; then
    fail "backup manifest identity does not match this update request"
    return 1
  fi

  entries="$(printf '%s' "$state" | grep -oE "$archive_re" || true)"
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    fn="$(printf '%s' "$e" | sed -E 's/.*"filename":"([^"]*)".*/\1/')"
    sha="$(printf '%s' "$e" | sed -E 's/.*"sha256":"([^"]*)".*/\1/')"
    size="$(printf '%s' "$e" | sed -E 's/.*"size_bytes":([0-9]+).*/\1/')"
    printf '%s\n' "$seen_files" | grep -qxF "$fn" \
      && { fail "backup manifest lists an archive more than once"; return 1; }
    case "$fn" in
      "jarvis_${manifest_ts}.sql.gz.enc") role=jarvis; jarvis_seen=$((jarvis_seen + 1)) ;;
      "litellm_${manifest_ts}.sql.gz.enc") role=litellm; litellm_seen=$((litellm_seen + 1)) ;;
      "pdfs_${manifest_ts}.tar.gz.enc") role=pdfs; pdfs_seen=$((pdfs_seen + 1)) ;;
      "secrets_${manifest_ts}.tar.gz.enc") role=secrets; secrets_seen=$((secrets_seen + 1)) ;;
      qdrant_*_"${manifest_ts}.snapshot.enc")
        collection="${fn#qdrant_}"; collection="${collection%_"${manifest_ts}".snapshot.enc}"
        role="qdrant:${collection}" ;;
      *) fail "archive does not match signed timestamp"; return 1 ;;
    esac
    printf '%s\n' "$seen_roles" | grep -qxF "$role" \
      && { fail "backup manifest contains a duplicate logical role"; return 1; }
    seen_files="${seen_files}${fn}"$'\n'; seen_roles="${seen_roles}${role}"$'\n'
    [ -f "${BACKUP_DIR}/${fn}" ] && [ ! -L "${BACKUP_DIR}/${fn}" ] && [ -s "${BACKUP_DIR}/${fn}" ] \
      || { fail "required backup archive is missing, empty, or unsafe"; return 1; }
    actual_size="$(stat -c%s "${BACKUP_DIR}/${fn}" 2>/dev/null || stat -f%z "${BACKUP_DIR}/${fn}" 2>/dev/null || echo 0)"
    actual_sha="$(sha256sum "${BACKUP_DIR}/${fn}" 2>/dev/null | cut -d' ' -f1)"
    [ "$actual_size" = "$size" ] && [ "$actual_sha" = "$sha" ] \
      || { fail "backup archive does not match its authenticated size/checksum"; return 1; }
  done <<< "$entries"
  if [ "$shape" = current ]; then
    [ "$jarvis_seen" -eq 1 ] && [ "$litellm_seen" -eq 1 ] \
      && [ "$pdfs_seen" -eq 1 ] && [ "$secrets_seen" -eq 1 ] \
      || { fail "safe update backup needs Jarvis, LiteLLM, PDFs, and secrets archives"; return 1; }
  else
    [ "$jarvis_seen" -eq 1 ] && [ "$litellm_seen" -eq 1 ] \
      && [ "$secrets_seen" -eq 1 ] \
      || { fail "legacy update backup needs Jarvis, LiteLLM, and secrets archives"; return 1; }
  fi
  for disk in "$BACKUP_DIR"/*_"${manifest_ts}".*; do
    [ -f "$disk" ] || continue
    base="$(basename "$disk")"
    case "$base" in "${manifest_base}"|"${manifest_base}.hmac") continue ;; esac
    printf '%s\n' "$seen_files" | grep -qxF "$base" \
      || { fail "restore point contains an undeclared archive"; return 1; }
  done
  printf '%s|%s\n' "$manifest_ts" "$signed_run_id"
}

# The schema version an authenticated manifest records for the installation it
# was taken from. Only meaningful once verify_backup_set has accepted the set.
manifest_schema_version() {
  grep -oE '"schema_version":[0-9]+' "$1" | head -1 | cut -d: -f2
}

wait_verify_backup() {
  local request_id="$1" timeout="$2" interval="$3" waited=0 manifest f
  valid_id "$request_id" || fail "invalid backup request id"
  valid_timeout "$timeout" || fail "invalid backup wait timeout"
  printf '%s' "$interval" | grep -Eq '^[1-9][0-9]{0,3}$' || fail "invalid backup poll interval"
  while [ "$waited" -le "$timeout" ]; do
    manifest=""
    for f in "$BACKUP_DIR"/manifest_*.json; do
      [ -f "$f" ] && [ -f "${f}.hmac" ] || continue
      if grep -qF "\"run_id\":\"${request_id}\"" "$f" 2>/dev/null; then
        manifest="$f"
        break
      fi
    done
    [ -z "$manifest" ] || { verify_backup_set "$manifest" "$request_id" current; return; }
    [ "$waited" -ge "$timeout" ] && break
    sleep "$interval"
    waited=$((waited + interval))
  done
  printf 'ERROR: no authenticated backup for this request appeared within %ss\n' \
    "$timeout" >&2
  return 75
}

# Inspect or acknowledge one exact off-host restore quarantine. Inspection
# validates the record before the host CLI prompts. Acknowledgement takes the
# trigger-volume lock, validates the record again, removes the matching browser
# token, and syncs each removal. Any incomplete acknowledgement leaves the
# quarantine active.
restore_quarantine_state() {
  local mode="$1" expected_id="$2"
  valid_id "$expected_id" || fail "invalid restore quarantine id"
  case "$mode" in inspect|acknowledge) ;; *) fail "invalid quarantine operation"; return ;; esac
  perl -MJSON::PP -MFcntl=:DEFAULT,:flock,:mode \
    -MIO::Handle -MTime::Local=timegm -MErrno=ENOENT \
    - "$mode" "$expected_id" "$TRIGGER_DIR" <<'PERL'
use strict;
use warnings;

my ($mode, $expected_id, $trigger_dir) = @ARGV;
my $max_bytes = 64 * 1024;
my $quarantine_path = "$trigger_dir/.outbound-quarantine.json";
my $capability_path = "$trigger_dir/.restore_status_token.json";
my $lock_path = "$trigger_dir/.restore_state.lock";

sub fail_state { die "restore quarantine state is unavailable or inconsistent\n"; }

sub timestamp_epoch {
    my ($value) = @_;
    $value =~ /\A([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?(Z|([+-])([0-9]{2}):([0-9]{2}))\z/
        or fail_state();
    my ($year, $month, $day, $hour, $minute, $second) = ($1, $2, $3, $4, $5, $6);
    my ($fraction, $zone, $sign, $offset_hour, $offset_minute) = ($7, $8, $9, $10, $11);
    $year >= 1 && $month >= 1 && $month <= 12 && $day >= 1
        && $hour <= 23 && $minute <= 59 && $second <= 59
        or fail_state();
    my $epoch = eval { timegm($second, $minute, $hour, $day, $month - 1, $year) };
    defined $epoch or fail_state();
    my @round_trip = gmtime($epoch);
    ($round_trip[5] + 1900 == $year && $round_trip[4] + 1 == $month
        && $round_trip[3] == $day && $round_trip[2] == $hour
        && $round_trip[1] == $minute && $round_trip[0] == $second)
        or fail_state();
    if ($zone ne "Z") {
        $offset_hour <= 23 && $offset_minute <= 59 or fail_state();
        my $offset = ($offset_hour * 60 + $offset_minute) * 60;
        $epoch += $sign eq "+" ? -$offset : $offset;
    }
    return $epoch + (defined $fraction ? "0.$fraction" : 0);
}

sub read_quarantine {
    sysopen(my $fh, $quarantine_path, O_RDONLY | O_NOFOLLOW)
        or fail_state();
    my @state = stat($fh);
    @state && S_ISREG($state[2]) && $state[3] == 1 && $state[7] <= $max_bytes
        or fail_state();
    my $raw = "";
    while (1) {
        my $chunk = "";
        my $read = sysread($fh, $chunk, $max_bytes + 1 - length($raw));
        defined $read or fail_state();
        last if $read == 0;
        $raw .= $chunk;
        length($raw) <= $max_bytes or fail_state();
    }
    close($fh) or fail_state();
    my $data = eval { JSON::PP->new->utf8->decode($raw) };
    ref($data) eq "HASH" or fail_state();
    my @expected_keys = qw(completed_at requested_at restore_id review_state source version);
    join("\0", sort keys %{$data}) eq join("\0", @expected_keys) or fail_state();
    for my $key (@expected_keys) {
        my $count = () = $raw =~ /"\Q$key\E"\s*:/g;
        $count == 1 or fail_state();
    }
    my $canonical = JSON::PP->new->canonical->encode($data);
    my $timestamp = qr/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})/;
    $canonical =~ /\A\{"completed_at":"($timestamp)","requested_at":"($timestamp)","restore_id":"([0-9a-f]{32})","review_state":"awaiting_review","source":"inbox","version":1\}\z/
        or fail_state();
    my ($completed_at, $requested_at, $restore_id) = ($1, $2, $3);
    $restore_id eq $expected_id or fail_state();
    timestamp_epoch($requested_at) <= timestamp_epoch($completed_at) or fail_state();
}

sub sync_trigger_dir {
    sysopen(my $dir_fh, $trigger_dir, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        or fail_state();
    $dir_fh->sync or fail_state();
    close($dir_fh) or fail_state();
}

my @trigger_state = lstat($trigger_dir);
@trigger_state && S_ISDIR($trigger_state[2]) && !S_ISLNK($trigger_state[2])
    or fail_state();

if ($mode eq "inspect") {
    read_quarantine();
    print "$expected_id\n";
    exit 0;
}

sysopen(my $lock_fh, $lock_path, O_RDWR | O_CREAT | O_NOFOLLOW, 0600)
    or fail_state();
my @lock_state = stat($lock_fh);
@lock_state && S_ISREG($lock_state[2]) && $lock_state[3] == 1 or fail_state();
flock($lock_fh, LOCK_EX) or fail_state();
read_quarantine();

my @capability_state = lstat($capability_path);
if (@capability_state) {
    !S_ISDIR($capability_state[2]) or fail_state();
    unlink($capability_path) or fail_state();
    sync_trigger_dir();
} elsif ($! != ENOENT) {
    fail_state();
}

read_quarantine();
unlink($quarantine_path) or fail_state();
sync_trigger_dir();
print "$expected_id\n";
PERL
}

usage() {
  printf '%s\n' 'usage: backup-lifecycle.sh <reserve-host|host-reservation-status|hold-host|host-status|wait-host|current-host|release-host|host-release-complete|cancel-host-reservation|clear-retained-host|reserve-update|update-reservation-status|current-update-reservation|hold-update|update-status|update-promoted-status|wait-update|current-update|promote-update|release-update|reserve-rotation|rotation-reservation-status|hold-rotation|rotation-status|wait-rotation|current-rotation|release-rotation|rotation-release-complete|inspect-quarantine|acknowledge-quarantine|publish-request|write-pin|pin-matches|clear-pin|wait-verify|verify|verify-floor> ...' >&2
  exit 2
}

case "${1:-}" in
  reserve-host)       [ "$#" -eq 3 ] || usage; reserve_host "$2" "$3" ;;
  host-reservation-status)
    [ "$#" -eq 3 ] || usage
    host_reservation_owner_status "$2" "$3"
    ;;
  hold-host)          [ "$#" -eq 4 ] || usage; hold_host "$2" "$3" "$4" ;;
  host-status)        [ "$#" -eq 3 ] || usage; host_guard_status "$2" "$3" ;;
  wait-host)          [ "$#" -eq 5 ] || usage; wait_host_activation "$2" "$3" "$4" "$5" ;;
  current-host)       [ "$#" -eq 1 ] || usage; current_host_operation ;;
  release-host)       [ "$#" -eq 4 ] || usage; release_host "$2" "$3" "$4" ;;
  host-release-complete)
    [ "$#" -eq 4 ] || usage
    host_release_complete "$2" "$3" "$4"
    ;;
  cancel-host-reservation)
    [ "$#" -eq 3 ] || usage
    cancel_host_reservation "$2" "$3"
    ;;
  clear-retained-host)
    [ "$#" -eq 3 ] || usage
    clear_retained_host "$2" "$3"
    ;;
  reserve-update)    [ "$#" -eq 2 ] || usage; reserve_update "$2" ;;
  update-reservation-status)
    [ "$#" -eq 2 ] || usage
    update_reservation_owner_status "$2"
    ;;
  current-update-reservation)
    [ "$#" -eq 1 ] || usage
    current_update_reservation
    ;;
  hold-update)       [ "$#" -eq 3 ] || usage; hold_update "$2" "$3" ;;
  update-status)     [ "$#" -eq 2 ] || usage; update_guard_status "$2" ;;
  update-promoted-status)
    [ "$#" -eq 2 ] || usage
    promoted_update_status "$2"
    ;;
  wait-update)       [ "$#" -eq 4 ] || usage; wait_update_activation "$2" "$3" "$4" ;;
  current-update)    [ "$#" -eq 1 ] || usage; current_update_guard ;;
  promote-update)    [ "$#" -eq 2 ] || usage; promote_update "$2" ;;
  release-update)
    { [ "$#" -eq 2 ] || [ "$#" -eq 3 ]; } || usage
    release_update "$2" "${3:-clear}"
    ;;
  reserve-rotation)  [ "$#" -eq 2 ] || usage; reserve_rotation "$2" ;;
  rotation-reservation-status)
    [ "$#" -eq 2 ] || usage
    rotation_reservation_owner_status "$2"
    ;;
  hold-rotation)     [ "$#" -eq 3 ] || usage; hold_rotation "$2" "$3" ;;
  rotation-status)   [ "$#" -eq 2 ] || usage; rotation_guard_status "$2" ;;
  wait-rotation)     [ "$#" -eq 4 ] || usage; wait_rotation_activation "$2" "$3" "$4" ;;
  current-rotation)  [ "$#" -eq 1 ] || usage; current_rotation_guard ;;
  release-rotation)  [ "$#" -eq 3 ] || usage; release_rotation "$2" "$3" ;;
  rotation-release-complete)
    [ "$#" -eq 3 ] || usage
    rotation_release_complete "$2" "$3"
    ;;
  inspect-quarantine)
    [ "$#" -eq 2 ] || usage
    restore_quarantine_state inspect "$2"
    ;;
  acknowledge-quarantine)
    [ "$#" -eq 2 ] || usage
    restore_quarantine_state acknowledge "$2"
    ;;
  publish-request)   [ "$#" -eq 2 ] || usage; publish_request "$2" ;;
  write-pin)         [ "$#" -eq 4 ] || usage; write_pin "$2" "$3" "$4" ;;
  pin-matches)       [ "$#" -eq 4 ] || usage; pin_matches "$2" "$3" "$4" ;;
  clear-pin)         [ "$#" -eq 4 ] || usage; clear_pin "$2" "$3" "$4" ;;
  wait-verify)       [ "$#" -eq 4 ] || usage; wait_verify_backup "$2" "$3" "$4" ;;
  verify)
    [ "$#" -eq 4 ] || usage
    valid_ts "$2" || fail "invalid backup timestamp"
    verify_backup_set "${BACKUP_DIR}/manifest_${2}.json" "$3" "$4"
    ;;
  verify-floor)
    [ "$#" -eq 4 ] || usage
    valid_ts "$2" || fail "invalid backup timestamp"
    verify_backup_set "${BACKUP_DIR}/manifest_${2}.json" "$3" "$4" >/dev/null \
      && manifest_schema_version "${BACKUP_DIR}/manifest_${2}.json"
    ;;
  *) usage ;;
esac
