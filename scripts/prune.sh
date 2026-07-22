#!/usr/bin/env bash
# prune.sh — JARVIS backup DELETE executor (runs in the postgres-backup sidecar,
# never the app, so the app gains ZERO new privilege). The app only writes the
# .delete_request.json sentinel into the shared backup_trigger volume; EVERY rm
# of an archive file happens here, in the already-privileged sidecar.
#
# Triggered by /backup-trigger/.delete_request.json (written by the admin API).
# It deletes the archive set of one or more explicitly named restore-point
# timestamps. Load-bearing safety invariants (each closes a specific failure mode):
#   1. AT-MOST-ONCE — the request sentinel is consumed (rm -f) BEFORE any
#      deletion, so a sidecar restart can never re-fire the delete.
#   2. VERSION GATE — an unknown request version is recorded and ignored (an old
#      sidecar must never mis-interpret a newer request shape and delete wrongly).
#   3. SERVER-SIDE RE-VALIDATION — candidate filenames are derived from the
#      /backups glob of the (digits-only) timestamp and re-checked through
#      valid_archive_name, the SAME allowlist restore.sh uses; a filename from the
#      request JSON is NEVER trusted, so a tampered timestamp can neither escape
#      /backups nor delete a non-archive file.
#   4. NEVER-DELETE-AN-IN-FLIGHT-RESTORE-OR-UPDATE — a timestamp a present
#      .restore_request.json / .restore_status.json is using (its target or its
#      safety backup), or an authenticated pending-update rollback pin reserves,
#      is refused. A delete can never pull a restore source or update rollback
#      point out from under the operation that needs it.
#   5. NEVER crash the sidecar loop — a benign already-gone is idempotent and the
#      script exits 0 on every path (a non-zero exit would crash-restart the
#      restart:unless-stopped sidecar into a re-delete loop).
#
# NOTE: -e is intentionally NOT set (a benign failure must not abort mid-run);
# pipefail is intentionally NOT set (grep -q closing a pipe early must not be
# misread as a failure and skip a safety check).

set -u
shopt -s nullglob

# --- Configuration -----------------------------------------------------------
TRIGGER_DIR="${BACKUP_TRIGGER_DIR:-/backup-trigger}"
REQUEST_FILE="${TRIGGER_DIR}/.delete_request.json"
RESTORE_REQUEST_FILE="${TRIGGER_DIR}/.restore_request.json"
RESTORE_STATUS_FILE="${TRIGGER_DIR}/.restore_status.json"
OUTCOME_FILE="${TRIGGER_DIR}/.last_delete.json"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
LOCK_DIR="${BACKUP_DIR}/.lifecycle"
UPDATE_PIN_FILE="${LOCK_DIR}/update-backup-pin.json"
UPDATE_LOCK="${LOCK_DIR}/update.lock"
SUPPORTED_VERSION=1

# --- valid_archive_name — a verbatim copy of restore.sh's function (itself a
#     mirror of backups.py:_FILENAME_RE). Rejects path separators / '..' and pins
#     the five archive shapes, so a tampered timestamp can never escape /backups.
#     Keep in sync with scripts/restore.sh and backups.py. ----------------------
valid_archive_name() {
  local n="$1"
  case "$n" in
    */*|*\\*|*..*) return 1 ;;
  esac
  printf '%s' "$n" | grep -Eq \
    '^(jarvis_[0-9]{8}_[0-9]{6}\.sql\.gz(\.enc)?|litellm_[0-9]{8}_[0-9]{6}\.sql\.gz(\.enc)?|pdfs_[0-9]{8}_[0-9]{6}\.tar\.gz(\.enc)?|secrets_[0-9]{8}_[0-9]{6}\.tar\.gz(\.enc)?|qdrant_[A-Za-z0-9_-]+_[0-9]{8}_[0-9]{6}\.snapshot(\.enc)?)$'
}

# --- is_deletable <basename> <validated_ts> — the 5 archive shapes (via
#     valid_archive_name) OR the manifest sidecar for this exact validated
#     timestamp. valid_archive_name deliberately rejects manifest_*.json, but a
#     full restore-point delete must also drop its manifest; the timestamp is
#     digits-only (validated below) so the constructed name is injection-safe. ---
is_deletable() {
  local base="$1" ts="$2"
  case "$base" in
    "manifest_${ts}.json"|"manifest_${ts}.json.hmac") return 0 ;;
  esac
  valid_archive_name "$base"
}

# --- restore_in_flight_ts — every timestamp a present restore or pending update
#     is using, one per line. Restore fields reserve a target and safety backup;
#     a narrowly validated update pin reserves its rollback point. Used to
#     refuse deleting a point either operation currently depends on. -----------
restore_in_flight_ts() {
  local f pin
  for f in "$RESTORE_REQUEST_FILE" "$RESTORE_STATUS_FILE"; do
    [ -f "$f" ] || continue
    grep -oE '"(timestamp|safety_backup_ts)"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' "$f" 2>/dev/null \
      | grep -oE '[0-9]{8}_[0-9]{6}' || true
  done
  # An update pin is machine-written state, but still validate its complete,
  # bounded shape before trusting it to protect an archive timestamp.
  if [ -f "$UPDATE_PIN_FILE" ] \
     && [ ! -L "$UPDATE_PIN_FILE" ] \
     && [ "$(stat -c%s "$UPDATE_PIN_FILE" 2>/dev/null || echo 9999)" -le 256 ] \
     && [ "$(wc -l < "$UPDATE_PIN_FILE" 2>/dev/null || echo 0)" -eq 1 ]; then
    pin="$(cat "$UPDATE_PIN_FILE" 2>/dev/null || true)"
    if printf '%s' "$pin" \
        | grep -Eq '^\{"timestamp":"[0-9]{8}_[0-9]{6}","run_id":"[0-9a-f]{32}"\}$' \
       || printf '%s' "$pin" \
        | grep -Eq '^\{"timestamp":"[0-9]{8}_[0-9]{6}","run_id":null,"legacy_recovery":true\}$'; then
      printf '%s\n' "$pin" | grep -oE '"timestamp":"[0-9]{8}_[0-9]{6}"' \
        | grep -oE '[0-9]{8}_[0-9]{6}'
    fi
  fi
}

# --- JSON helpers (small, no jq in the image) --------------------------------
_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

# json_array_from_lines — newline-delimited stdin -> a JSON array of strings.
json_array_from_lines() {
  local first=1 line out="["
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    if [ "$first" = "1" ]; then first=0; else out="${out},"; fi
    out="${out}\"$(_json_escape "$line")\""
  done
  out="${out}]"
  printf '%s' "$out"
}

# deleted/skipped accumulate as newline-delimited strings; write_outcome renders
# them as JSON arrays into .last_delete.json (atomic .tmp -> mv).
deleted=""
skipped=""

# write_outcome <reason-or-empty> — record the outcome; never abort the script.
write_outcome() {
  local reason="$1" tmp="${OUTCOME_FILE}.tmp" reason_json d_json s_json
  d_json="$(printf '%s\n' "$deleted" | json_array_from_lines)"
  s_json="$(printf '%s\n' "$skipped" | json_array_from_lines)"
  if [ -z "$reason" ]; then reason_json="null"; else reason_json="\"$(_json_escape "$reason")\""; fi
  printf '{"deleted":%s,"skipped":%s,"at":"%s","reason":%s}' \
    "$d_json" "$s_json" "$(date -Iseconds)" "$reason_json" > "$tmp" 2>/dev/null || return 0
  mv -f "$tmp" "$OUTCOME_FILE" 2>/dev/null || return 0
}

# add_skipped <timestamp> <reason> — append "TS (reason)" to the skipped list.
add_skipped() {
  skipped="${skipped}${skipped:+$'\n'}$1 ($2)"
}

# === STEP 1: serialize with update backup selection/pinning ===================
# The updater holds this sidecar-private lock from rollback-point selection until
# health-verified commit. If it is busy, leave the request untouched so the
# sidecar loop retries after the update has either published a pin or finished.
mkdir -p "$TRIGGER_DIR" 2>/dev/null || exit 0
[ ! -L "$LOCK_DIR" ] || exit 0
mkdir -p "$LOCK_DIR" 2>/dev/null || exit 0
[ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] || exit 0
chmod 700 "$LOCK_DIR" 2>/dev/null || exit 0
[ ! -L "$UPDATE_LOCK" ] || exit 0
exec 7>>"$UPDATE_LOCK" || exit 0
if ! flock -n 7; then
  echo "[prune] update lifecycle is selecting or using a rollback point; retrying later" >&2
  exit 0
fi

# === STEP 2: consume the request FIRST (at-most-once) =========================
REQ_CONTENT="$(cat "$REQUEST_FILE" 2>/dev/null || true)"
rm -f "$REQUEST_FILE" 2>/dev/null || true

# Benign already-gone: the request vanished (or was empty). Nothing to do; never
# crash and never write a misleading outcome.
if [ -z "$REQ_CONTENT" ]; then
  exit 0
fi

# The request timestamps are the only 8_6-digit tokens in the JSON (requested_at
# is ISO with dashes/colons; version is a bare int) — so this extraction both
# parses AND shape-validates them.
timestamps="$(printf '%s' "$REQ_CONTENT" | grep -oE '[0-9]{8}_[0-9]{6}' | sort -u)"

# === STEP 3: version gate ====================================================
# An absent or non-1 version means an old sidecar met a newer request shape:
# record the requested timestamps as skipped and ignore rather than mis-delete.
version="$(printf '%s' "$REQ_CONTENT" | grep -oE '"version"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' | head -1)"
if [ "${version:-x}" != "$SUPPORTED_VERSION" ]; then
  skipped="$timestamps"
  write_outcome "unknown version"
  exit 0
fi

# === STEP 4: confirm gate ====================================================
if ! printf '%s' "$REQ_CONTENT" | grep -qE '"confirm"[[:space:]]*:[[:space:]]*"DELETE"'; then
  skipped="$timestamps"
  write_outcome "confirm required"
  exit 0
fi

# === STEP 5: per-timestamp delete ============================================
in_flight="$(restore_in_flight_ts | sort -u)"

while IFS= read -r ts; do
  [ -n "$ts" ] || continue

  # Refuse a timestamp a live restore or update is using.
  if grep -qxF "$ts" <<<"$in_flight"; then
    add_skipped "$ts" "in-flight restore or update"
    continue
  fi

  matched=0
  for f in "$BACKUP_DIR"/*_"${ts}".*; do
    base="$(basename "$f")"
    is_deletable "$base" "$ts" || continue   # server-side re-validation
    if rm -f -- "$f" 2>/dev/null; then
      deleted="${deleted}${deleted:+$'\n'}${base}"
      matched=1
    fi
  done
  [ "$matched" = "0" ] && add_skipped "$ts" "no matching files"
done <<EOF
$timestamps
EOF

# === STEP 6: record the outcome ==============================================
write_outcome ""
exit 0
