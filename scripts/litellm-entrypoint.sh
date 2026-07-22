#!/bin/sh
# Keep LiteLLM stopped while restore safety or outbound-review markers exist.

set -eu

litellm_restore_hold_active() {
  trigger_dir="${LITELLM_TRIGGER_DIR:-/backup-trigger}"
  for marker in .maintenance .destructive .outbound-quarantine.json; do
    marker_path="${trigger_dir}/${marker}"
    if [ -e "$marker_path" ] || [ -L "$marker_path" ]; then
      return 0
    fi
  done
  return 1
}

wait_for_restore_release() {
  announced=0
  while litellm_restore_hold_active; do
    if [ "$announced" -eq 0 ]; then
      echo "[litellm] Restore safety marker present; waiting before loading provider keys." >&2
      announced=1
    fi
    sleep "${LITELLM_WATCH_INTERVAL_SECONDS:-1}"
  done
}

litellm_healthcheck() {
  if litellm_restore_hold_active; then
    # The process is intentionally paused so recovery services may start while
    # provider access remains unavailable.
    return 0
  fi
  python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:4000/health/liveliness", timeout=3).read()' \
    >/dev/null 2>&1
}

read_rotation_marker() {
  marker="${LITELLM_TRIGGER_DIR:-/backup-trigger}/.secrets_rotated"
  rotation_marker_value=0
  [ -r "$marker" ] || return 0

  first_line=""
  seen_first_line=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$seen_first_line" -eq 0 ]; then
      first_line="$line"
      seen_first_line=1
    elif [ -n "$line" ]; then
      return 0
    fi
  done < "$marker"

  case "$first_line" in
    ''|*[!0-9]*) ;;
    *) rotation_marker_value="$first_line" ;;
  esac
}

stop_child() {
  signal="$1"
  if [ -n "${litellm_pid:-}" ]; then
    kill "-$signal" "$litellm_pid" 2>/dev/null || true
  fi
}

forward_signal() {
  signal="$1"
  status="$2"
  trap - TERM INT
  stop_child "$signal"
  [ -z "${watcher_pid:-}" ] || kill "$watcher_pid" 2>/dev/null || true
  [ -z "${litellm_pid:-}" ] || wait "$litellm_pid" 2>/dev/null || true
  [ -z "${watcher_pid:-}" ] || wait "$watcher_pid" 2>/dev/null || true
  exit "$status"
}

load_litellm_configuration() {
  secret_dir="${LITELLM_SECRET_DIR:-/run/secrets}"
  master_key_file="${secret_dir}/litellm_master_key"
  salt_key_file="${secret_dir}/litellm_salt_key"
  postgres_password_file="${secret_dir}/postgres_password"

  if [ ! -s "$master_key_file" ]; then
    echo "FATAL: ${master_key_file} is empty or missing." >&2
    return 1
  fi
  export LITELLM_MASTER_KEY="$(cat "$master_key_file")"

  if [ "${ENVIRONMENT:-development}" = "production" ]; then
    case "$LITELLM_MASTER_KEY" in
      "sk-jarvis-dev-test"|changeme|secret|password|""|"sk-1234")
        echo "FATAL: LITELLM_MASTER_KEY is a known placeholder value." >&2
        echo "Replace secrets/litellm_master_key.txt before deploying to production." >&2
        return 1
        ;;
    esac
    if [ "${#LITELLM_MASTER_KEY}" -lt 16 ]; then
      echo "FATAL: LITELLM_MASTER_KEY is too short for production." >&2
      return 1
    fi
  fi

  if [ ! -s "$salt_key_file" ]; then
    echo "FATAL: ${salt_key_file} is empty or missing." >&2
    return 1
  fi
  export LITELLM_SALT_KEY="$(cat "$salt_key_file")"

  if [ ! -s "$postgres_password_file" ]; then
    echo "FATAL: ${postgres_password_file} is empty or missing." >&2
    return 1
  fi
  postgres_user_encoded="$(
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' \
      "${POSTGRES_USER:-jarvis}"
  )"
  postgres_password_encoded="$(
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' \
      "$(cat "$postgres_password_file")"
  )"
  export DATABASE_URL="postgresql://${postgres_user_encoded}:${postgres_password_encoded}@postgres:5432/litellm"
}

run_litellm() {
  read_rotation_marker
  rotation_at_start="$rotation_marker_value"
  litellm --config /app/config.yaml &
  litellm_pid=$!
  watcher_pid=""
  trap 'forward_signal TERM 143' TERM
  trap 'forward_signal INT 130' INT

  (
    while kill -0 "$litellm_pid" 2>/dev/null; do
      sleep "${LITELLM_WATCH_INTERVAL_SECONDS:-1}"
      if litellm_restore_hold_active; then
        echo "[litellm] Restore safety marker detected; stopping provider access." >&2
        kill -TERM "$litellm_pid" 2>/dev/null || true
        exit 0
      fi
      read_rotation_marker
      rotation_now="$rotation_marker_value"
      if [ "$rotation_now" != "$rotation_at_start" ]; then
        echo "[litellm] Provider secrets changed; restarting to reload them." >&2
        kill -TERM "$litellm_pid" 2>/dev/null || true
        exit 0
      fi
    done
  ) &
  watcher_pid=$!

  set +e
  wait "$litellm_pid"
  status=$?
  set -e
  kill "$watcher_pid" 2>/dev/null || true
  wait "$watcher_pid" 2>/dev/null || true
  trap - TERM INT
  return "$status"
}

main() {
  while true; do
    wait_for_restore_release
    load_litellm_configuration || exit 1
    if litellm_restore_hold_active; then
      unset LITELLM_MASTER_KEY LITELLM_SALT_KEY DATABASE_URL
      continue
    fi
    run_litellm
    return $?
  done
}

case "${1:-}" in
  --functions-only) return 0 2>/dev/null || exit 0 ;;
  --healthcheck) litellm_healthcheck; exit $? ;;
esac

main "$@"
