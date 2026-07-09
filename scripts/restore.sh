#!/usr/bin/env bash
# restore.sh — JARVIS one-click disaster-recovery RESTORE (runs in the
# postgres-backup sidecar, never the app, so the app gains ZERO new privilege).
#
# Triggered by /backup-trigger/.restore_request.json (written by the admin API).
# It restores a chosen backup timestamp: safety pre-backup -> drop+recreate+
# reload both Postgres DBs -> recover Qdrant -> all behind a maintenance gate.
#
# Three load-bearing safety invariants (each closes a specific failure mode):
#   1. AT-MOST-ONCE — the request sentinel is consumed (rm -f) BEFORE any
#      destruction, so a failed restore can never re-fire on a sidecar restart.
#      A recorded terminal failure exits 0 (the sidecar is restart:unless-stopped;
#      a non-zero exit would crash-restart into a re-drop loop).
#   2. QUIESCE-BY-REVOKE — every DROP DATABASE is preceded by
#      `ALTER DATABASE ... ALLOW_CONNECTIONS false` + pg_terminate_backend, so
#      the persistent litellm container and the app pools cannot re-grab the DB
#      during the drop window (the sidecar has no docker.sock to stop them).
#   3. NEVER-RE-EXPOSE-A-DESTROYED-DB — a failure AFTER the first DROP keeps the
#      maintenance sentinel ON (the stack stays 503) until the operator restores
#      from the safety backup. A clean restore, or any failure BEFORE the first
#      DROP (nothing was destroyed), clears maintenance immediately.

set -euo pipefail

# --- Configuration -----------------------------------------------------------
TRIGGER_DIR="${BACKUP_TRIGGER_DIR:-/backup-trigger}"
REQUEST_FILE="${TRIGGER_DIR}/.restore_request.json"
STATUS_FILE="${TRIGGER_DIR}/.restore_status.json"
MAINTENANCE_SENTINEL="${MAINTENANCE_SENTINEL:-${TRIGGER_DIR}/.maintenance}"
MAINTENANCE_DESTRUCTIVE="${MAINTENANCE_DESTRUCTIVE_SENTINEL:-${TRIGGER_DIR}/.destructive}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
# Off-host (cross-host) disaster recovery: the operator drops the archive set +
# a one-time operator key into the rw restore_inbox volume mounted here. This is
# the ONLY writable cross-host secrets-staging surface and is NEVER under the RO
# /secrets mount. Used only by an `inbox` restore (see STEP 1 / STEP 8); the
# same-host WebUI restore never touches any of these paths.
INBOX_DIR="${RESTORE_INBOX_DIR:-/restore-inbox}"
OPERATOR_KEYFILE="${INBOX_DIR}/operator_key"
SECRETS_STAGING="${INBOX_DIR}/.secrets-staging"

PGHOST="${PGHOST:-postgres}"
PGUSER="${PGUSER:-jarvis}"
JARVIS_DB="${PGDATABASE:-jarvis}"
LITELLM_DB="${LITELLM_DATABASE:-litellm}"
ENC_KEYFILE="${BACKUP_ENCRYPT_KEYFILE:-}"
QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"
QDRANT_API_KEYFILE="${QDRANT_API_KEYFILE:-/run/secrets/qdrant_api_key}"
# Shared staging dir: the sidecar writes the decrypted snapshot here and Qdrant
# reads it via file:// — both containers mount the restore_staging volume at this
# same path (under Qdrant's default /qdrant/snapshots dir).
QDRANT_STAGING_DIR="/qdrant/snapshots/restore"

# --- Restore state (drives the .restore_status.json the FE polls) ------------
STATE="running"
CURRENT_STEP=""
ERROR=""
SAFETY_BACKUP_TS=""
STARTED_AT="$(date -Iseconds)"
FINISHED_AT=""
STEP_SAFETY="pending"
STEP_DB="pending"
STEP_LITELLM="pending"
STEP_QDRANT="pending"
STEP_FINISH="pending"
DROP_STARTED=0
RESTORE_CLEAN=0
RESTORE_OLDER=0
HEARTBEAT_PID=""
MANUAL_STEPS_REQUIRED=0
PHASE=""
# Restore source: "local" (same-host WebUI restore, the default) reads the
# archive set + key exactly as before; "inbox" (off-host DR) reads them from the
# rw restore_inbox volume and additionally materializes secrets + rebinds the
# postgres role. Both are set from the request in STEP 1; the inbox seams are all
# guarded by [ "$SOURCE" = "inbox" ] so the local path is byte-for-byte unchanged.
SOURCE="local"
ARCHIVE_DIR="$BACKUP_DIR"

# --- JSON status writer (atomic .tmp -> mv; matches the P6.3 RestoreStatus
#     shape: state/current_step/steps[].{name,status}/safety_backup_ts/
#     started_at/finished_at/error). Never aborts the script (|| return 0). -----
_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

_json_or_null() {
  if [ -z "$1" ]; then printf 'null'; else printf '"%s"' "$(_json_escape "$1")"; fi
}

write_status() {
  local tmp="${STATUS_FILE}.tmp" drop_json manual_json
  if [ "$DROP_STARTED" = "1" ]; then drop_json="true"; else drop_json="false"; fi
  if [ "$MANUAL_STEPS_REQUIRED" = "1" ]; then manual_json="true"; else manual_json="false"; fi
  {
    printf '{"state":"%s","current_step":%s,"steps":[' "$STATE" "$(_json_or_null "$CURRENT_STEP")"
    printf '{"name":"Safety backup","status":"%s"},' "$STEP_SAFETY"
    printf '{"name":"Restoring database","status":"%s"},' "$STEP_DB"
    printf '{"name":"Restoring API-key store","status":"%s"},' "$STEP_LITELLM"
    printf '{"name":"Restoring search index","status":"%s"},' "$STEP_QDRANT"
    printf '{"name":"Finishing up","status":"%s"}],' "$STEP_FINISH"
    printf '"safety_backup_ts":%s,"started_at":%s,"finished_at":%s,"error":%s,"drop_started":%s,"manual_steps_required":%s,"phase":%s}' \
      "$(_json_or_null "$SAFETY_BACKUP_TS")" "$(_json_or_null "$STARTED_AT")" \
      "$(_json_or_null "$FINISHED_AT")" "$(_json_or_null "$ERROR")" "$drop_json" \
      "$manual_json" "$(_json_or_null "$PHASE")"
  } > "$tmp" 2>/dev/null || return 0
  mv -f "$tmp" "$STATUS_FILE" 2>/dev/null || return 0
}

# --- decrypt_or_passthrough — the INVERSE of backup.sh:encrypt_or_passthrough.
#     With a file arg ending in .enc it openssl-decrypts that file to stdout
#     (same cipher params backup.sh encrypts with); any other file is cat'd; with
#     no arg it passes stdin straight through. -----------------------------------
decrypt_or_passthrough() {
  local f="${1:-}"
  if [ -z "$f" ]; then
    cat
    return
  fi
  case "$f" in
    *.enc) openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -kfile "$ENC_KEYFILE" -in "$f" ;;
    *)     cat -- "$f" ;;
  esac
}

# --- valid_archive_name — mirror of backups.py:_FILENAME_RE. Rejects path
#     separators / '..' and pins the four archive shapes, so a tampered timestamp
#     in the request can never escape /backups (e.g. into /run/secrets/*). --------
valid_archive_name() {
  local n="$1"
  case "$n" in
    */*|*\\*|*..*) return 1 ;;
  esac
  printf '%s' "$n" | grep -Eq \
    '^(jarvis_[0-9]{8}_[0-9]{6}\.sql\.gz(\.enc)?|litellm_[0-9]{8}_[0-9]{6}\.sql\.gz(\.enc)?|secrets_[0-9]{8}_[0-9]{6}\.tar\.gz(\.enc)?|qdrant_[A-Za-z0-9_-]+_[0-9]{8}_[0-9]{6}\.snapshot(\.enc)?)$'
}

# --- qdrant_http_body — an EXTENDED copy of backup.sh:qdrant_http (the image has
#     no curl). Unlike the lifted version it sends a JSON request body + a
#     Content-Type header, which the snapshot `recover` PUT requires — a
#     body-less PUT would 4xx and silently fail. ----------------------------------
qdrant_http_body() {
  QDRANT_URL="$QDRANT_URL" QDRANT_API_KEY="$QDRANT_API_KEY" \
  perl -MHTTP::Tiny -e '
    my ($method, $path, $body) = @ARGV;
    my %h;
    $h{"api-key"} = $ENV{QDRANT_API_KEY} if length $ENV{QDRANT_API_KEY};
    my %opts = ( headers => \%h );
    if (defined $body && length $body) {
      $opts{content} = $body;
      $h{"Content-Type"} = "application/json";
    }
    my $res = HTTP::Tiny->new(timeout => 600)->request(
      $method, $ENV{QDRANT_URL} . $path, \%opts);
    if (!$res->{success}) {
      print STDERR "qdrant " . $res->{status} . " " . ($res->{reason} // "") . "\n";
      exit 1;
    }
  ' "$@"
}

# psql_admin — run a statement against the `postgres` maintenance DB (so the
# DROP/CREATE of a product DB is never blocked by our own connection to it).
psql_admin() {
  psql -h "$PGHOST" -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -tAc "$1"
}

# restore_one_db <db> <archive> — quiesce-by-revoke, drop+recreate, reload.
restore_one_db() {
  local db="$1" archive="$2" st
  # (a) revoke CONNECT so litellm/app pools cannot re-grab the DB mid-window.
  #     A FAILED ALTER leaves the DB fully servable (untouched), so it returns
  #     WITHOUT marking the destructive window -> the lift gate clears maintenance.
  psql_admin "ALTER DATABASE \"${db}\" WITH ALLOW_CONNECTIONS false;" || return 1
  # The ALTER succeeded -> the DB is now NON-SERVABLE (rejects all connections), so
  # the destructive window has begun. Mark it BEFORE the terminate/DROP so ANY
  # later failure (even pg_terminate_backend) holds maintenance — never lift the
  # 503 over a DB left ALLOW_CONNECTIONS=false or already dropped.
  DROP_STARTED=1
  touch "$MAINTENANCE_DESTRUCTIVE" 2>/dev/null || true   # durable, never heartbeated
  write_status
  # (b) terminate the backends that were already connected.
  psql_admin "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${db}' AND pid <> pg_backend_pid();" >/dev/null || return 1
  # (c) DROP + CREATE (CREATE defaults ALLOW_CONNECTIONS true -> reachable for the
  #     reload; owner = the connecting jarvis role, matching --no-owner dumps).
  psql_admin "DROP DATABASE \"${db}\";" >/dev/null || return 1
  psql_admin "CREATE DATABASE \"${db}\";" >/dev/null || return 1
  # (d) reload the PLAIN-SQL dump (decrypt | gunzip | psql — NOT pg_restore).
  set +e
  decrypt_or_passthrough "$archive" | gunzip | psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -q >/dev/null
  st=("${PIPESTATUS[@]}")
  set -e
  [ "${st[0]}" -eq 0 ] && [ "${st[1]}" -eq 0 ] && [ "${st[2]}" -eq 0 ]
}

# Terminal failure BEFORE any destruction: record + exit 0 (nothing dropped).
fail_before_destruction() {
  STATE="failed"
  ERROR="$1"
  FINISHED_AT="$(date -Iseconds)"
  exit 0
}

# Terminal failure in STEP 5. If the destructive window was entered
# (DROP_STARTED=1) the DB is inconsistent and the EXIT trap keeps maintenance ON;
# if it failed before that (e.g. the very first ALTER) the DB is untouched and the
# trap clears maintenance — so word the message accordingly.
step5_fail() {
  STATE="failed"
  if [ "$DROP_STARTED" = "1" ]; then
    if [ "$SOURCE" = "inbox" ]; then
      ERROR="off-host restore failed mid-reload on a fresh host — re-run the off-host recovery per the runbook"
    else
      ERROR="database inconsistent — restore from safety backup ${SAFETY_BACKUP_TS:-<unknown>}"
    fi
  else
    ERROR="restore could not start; the database was not modified"
  fi
  FINISHED_AT="$(date -Iseconds)"
  exit 0
}

# Terminal failure in STEP 8 (off-host secrets materialization / role rebind),
# AFTER the DBs were already restored: the restored data is intact, but the
# cross-host secrets staging or the postgres-role rebind did not complete. Record
# it (exit 0) and let the EXIT trap hold maintenance (DROP_STARTED=1) so the stack
# stays 503 until the operator finishes the secrets/role steps per the runbook.
fail_after_restore() {
  STATE="failed"
  ERROR="$1"
  FINISHED_AT="$(date -Iseconds)"
  exit 0
}

# purge_secrets_staging — shred + remove the cross-host secrets staging (a full
# plaintext secret bundle on the rw inbox). shred the files first: a bare rm
# leaves block-level residue on journaling/overlay filesystems. Idempotent and
# || true so it can never abort the EXIT trap.
purge_secrets_staging() {
  [ -d "$SECRETS_STAGING" ] || return 0
  find "$SECRETS_STAGING" -type f -exec shred -u {} + 2>/dev/null || true
  rm -rf "$SECRETS_STAGING" 2>/dev/null || true
}

# --- EXIT trap: single terminal-status writer + maintenance lift gate ---------
_cleanup() {
  set +e
  [ -n "$HEARTBEAT_PID" ] && kill "$HEARTBEAT_PID" 2>/dev/null
  # belt-and-braces: re-consume the request so it can never re-fire.
  rm -f "$REQUEST_FILE" 2>/dev/null
  # Off-host DR hygiene: shred the one-time operator key + the plaintext secrets
  # staging on every clean or recorded-failure exit, so a failed restore never
  # leaves them on the rw restore_inbox volume. A SIGKILL cannot run this trap, so
  # it is not absolute. The guard fires on the inbox source OR whenever the key /
  # staging actually exist — so a malformed `source` field that defaults to "local"
  # still shreds an operator key the operator dropped. Harmless for a true local
  # restore (those paths never exist). Idempotent + || true so it never aborts.
  if [ "$SOURCE" = "inbox" ] || [ -e "$OPERATOR_KEYFILE" ] || [ -d "$SECRETS_STAGING" ]; then
    if command -v shred >/dev/null 2>&1; then
      shred -u "$OPERATOR_KEYFILE" 2>/dev/null || rm -f "$OPERATOR_KEYFILE" 2>/dev/null || true
    else
      rm -f "$OPERATOR_KEYFILE" 2>/dev/null || true
    fi
    purge_secrets_staging
  fi
  if [ "$STATE" = "running" ]; then
    STATE="failed"
    if [ -z "$ERROR" ]; then
      if [ -f "${TRIGGER_DIR}/.restore_timeout" ]; then
        if [ "$DROP_STARTED" = "1" ]; then
          ERROR="restore exceeded its time limit and was abandoned; the database may be inconsistent — restore from the safety backup ${SAFETY_BACKUP_TS:-<unknown>}"
        else
          ERROR="restore exceeded its time limit and was abandoned; nothing was destroyed"
        fi
      elif [ "$DROP_STARTED" = "1" ]; then
        if [ "$SOURCE" = "inbox" ]; then
          ERROR="off-host restore failed mid-reload on a fresh host — re-run the off-host recovery per the runbook"
        else
          ERROR="database inconsistent — restore from safety backup ${SAFETY_BACKUP_TS:-<unknown>}"
        fi
      else
        ERROR="restore terminated unexpectedly"
      fi
    fi
  fi
  [ -n "$FINISHED_AT" ] || FINISHED_AT="$(date -Iseconds)"
  write_status
  # Lift maintenance on a clean SAME-HOST restore OR any failure BEFORE the first
  # DROP (DROP_STARTED=0 => nothing was destroyed => safe to serve, true on a fresh
  # host too). A clean INBOX restore is deliberately NOT lifted here: on a fresh
  # host the app containers still hold the NEW-host postgres password while ALTER
  # ROLE rebound the live role to the OLD one, so the stack is not servable until
  # the operator materializes ./secrets and recreates the app containers — the
  # runbook clears BOTH sentinels as its final step. The clean inbox path enters the
  # destructive window (DROP_STARTED=1), so the durable .destructive sentinel is held
  # and does NOT auto-expire (the MAINTENANCE_MAX_AGE_S soft-expiry covers .maintenance
  # only) — the operator MUST clear it explicitly. Only a failure AFTER the first DROP
  # otherwise holds the sentinel (the DB is inconsistent). This can't race a
  # concurrent restore: the restore-request POST is itself 503'd while the sentinel
  # is up, so a post-DROP hold is never lifted by a later run.
  if { [ "$RESTORE_CLEAN" = "1" ] && [ "$SOURCE" != "inbox" ] && [ "$RESTORE_OLDER" != "1" ]; } || [ "$DROP_STARTED" = "0" ]; then
    rm -f "$MAINTENANCE_SENTINEL" 2>/dev/null
    rm -f "$MAINTENANCE_DESTRUCTIVE" 2>/dev/null
  fi
  rm -f "${TRIGGER_DIR}/.restore_timeout" 2>/dev/null || true
  # Never crash-restart the sidecar: a recorded terminal failure exits 0.
  exit 0
}
trap _cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# === STEP 1: consume the request FIRST (at-most-once) + validate =============
REQ_CONTENT="$(cat "$REQUEST_FILE" 2>/dev/null || true)"
rm -f "$REQUEST_FILE" 2>/dev/null || true
rm -f "${TRIGGER_DIR}/.restore_timeout" 2>/dev/null || true

TIMESTAMP="$(printf '%s' "$REQ_CONTENT" \
  | grep -oE '"timestamp"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' \
  | grep -oE '[0-9]{8}_[0-9]{6}' | head -1 || true)"

# source defaults to "local" when the field is absent (same-host WebUI restore);
# a present-but-unsupported value must fail safe, so distinguish "absent" (-> local)
# from "present and wrong" (-> fail) rather than silently defaulting the latter.
SOURCE_RAW="$(printf '%s' "$REQ_CONTENT" \
  | grep -oE '"source"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | sed -E 's/.*"source"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' | head -1 || true)"
SOURCE="${SOURCE_RAW:-local}"

write_status

if ! printf '%s' "$TIMESTAMP" | grep -Eq '^[0-9]{8}_[0-9]{6}$'; then
  fail_before_destruction "restore request did not name a valid backup timestamp"
fi

# Resolve the source BEFORE any archive lookup or destruction. local reads the
# /backups archive set with the same-host backup_encrypt_key (unchanged). inbox
# reads the operator-supplied archive set + one-time key from the rw restore_inbox
# volume; the operator key is validated here (pre-destruction) so a missing key
# fails safe, and the STEP-2 decrypt probe then proves it actually decrypts the DB
# archives before any DROP.
case "$SOURCE" in
  local) ;;
  inbox)
    ARCHIVE_DIR="$INBOX_DIR"
    ENC_KEYFILE="$OPERATOR_KEYFILE"
    if [ ! -s "$OPERATOR_KEYFILE" ]; then
      fail_before_destruction "off-host restore: operator key ${OPERATOR_KEYFILE} is missing or empty; drop the one-time backup key into the restore_inbox before requesting an inbox restore"
    fi
    ;;
  *)
    fail_before_destruction "restore request named an unsupported source '${SOURCE}' (expected local or inbox); nothing was changed"
    ;;
esac

JARVIS_ARCHIVE=""
LITELLM_ARCHIVE=""
QDRANT_SNAPS=()
shopt -s nullglob
for f in "${ARCHIVE_DIR}"/*_"${TIMESTAMP}".*; do
  base="$(basename "$f")"
  valid_archive_name "$base" || continue
  case "$base" in
    jarvis_*) JARVIS_ARCHIVE="$f" ;;
    litellm_*) LITELLM_ARCHIVE="$f" ;;
    qdrant_*) QDRANT_SNAPS+=("$f") ;;
  esac
done
shopt -u nullglob

if [ -z "$JARVIS_ARCHIVE" ] || [ -z "$LITELLM_ARCHIVE" ]; then
  fail_before_destruction "backup ${TIMESTAMP} is incomplete (missing a required database archive)"
fi

# --- PGPASSWORD (read AFTER consuming the request so a missing secret records a
#     terminal failure instead of crash-looping on the un-consumed sentinel). ----
if [ ! -r /run/secrets/postgres_password ]; then
  fail_before_destruction "cannot read the postgres password secret; restore aborted"
fi
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD

# === STEP 2: compat gate (defense-in-depth, BEFORE any destruction) ==========
MANIFEST="${ARCHIVE_DIR}/manifest_${TIMESTAMP}.json"
if [ -r "$MANIFEST" ]; then
  MANIFEST_CONTENT="$(cat "$MANIFEST" 2>/dev/null || true)"
  CHECKFILE="$(mktemp)"
  printf '%s' "$MANIFEST_CONTENT" \
    | grep -oE '"filename":"[^"]+","sha256":"[0-9a-f]{64}"' \
    | sed -E 's/"filename":"([^"]+)","sha256":"([0-9a-f]{64})"/\2  \1/' > "$CHECKFILE"
  if [ ! -s "$CHECKFILE" ]; then
    rm -f "$CHECKFILE"
    fail_before_destruction "manifest_${TIMESTAMP}.json is present but corrupt or incomplete (no archive checksums); nothing was changed"
  fi
  set +e
  ( cd "$ARCHIVE_DIR" && sha256sum -c --strict "$CHECKFILE" >/dev/null 2>&1 )
  SHA_RC=$?
  set -e
  if [ "$SHA_RC" -ne 0 ]; then
    rm -f "$CHECKFILE"
    fail_before_destruction "backup integrity check failed (sha256 mismatch); nothing was changed"
  fi
  rm -f "$CHECKFILE"

  MANIFEST_SCHEMA="$(printf '%s' "$MANIFEST_CONTENT" \
    | grep -oE '"schema_version"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
  # Compare the backup's schema against the CODE this deployment ships, NOT the
  # live DB. The live DB is the wrong reference: after a partial restore failure
  # the jarvis DB is gone/empty, so a live-schema query would read 0 and refuse
  # EVERY backup — bricking the very safety-backup recovery this script directs
  # the operator to run. The code's max schema is stable regardless of DB state
  # and is the right "what can this deployment load" bound. After a schema squash
  # db/migrations/ is empty (the baseline lives in init.sql), so the glob yields
  # nothing; in that case fall back to db/SCHEMA_VERSION — the single source of
  # the baseline floor — so this gate stays ARMED rather than failing open.
  if [ -n "$MANIFEST_SCHEMA" ]; then
    MIG_DIR="${MIGRATIONS_DIR:-/app/db/migrations}"
    CODE_MAX="$(ls "$MIG_DIR"/*.sql 2>/dev/null \
      | sed -E 's#.*/0*([0-9]+)_.*#\1#' | grep -E '^[0-9]+$' | sort -n | tail -1 || true)"
    if [ -z "$CODE_MAX" ]; then
      CODE_MAX="$(cat "${SCHEMA_VERSION_FILE:-${MIG_DIR%/migrations}/SCHEMA_VERSION}" 2>/dev/null \
        | tr -dc '0-9' || true)"
      [ -n "$CODE_MAX" ] || CODE_MAX="$(cat /app/db/SCHEMA_VERSION 2>/dev/null | tr -dc '0-9' || true)"
      [ -n "$CODE_MAX" ] || CODE_MAX=101
    fi
    if [ -n "$CODE_MAX" ] && [ "$MANIFEST_SCHEMA" -gt "$CODE_MAX" ]; then
      fail_before_destruction "backup is newer than this deployment (schema ${MANIFEST_SCHEMA} > code ${CODE_MAX}); upgrade JARVIS before restoring"
    elif [ -n "$CODE_MAX" ] && [ "$MANIFEST_SCHEMA" -lt "$CODE_MAX" ]; then
      RESTORE_OLDER=1
    fi
  fi
else
  echo "[restore] WARN: manifest ${MANIFEST} absent; proceeding (the admin endpoint pre-checks compatibility)" >&2
fi

# Pre-destruction decrypt probe: verify each DB archive decrypts to a valid gzip
# stream (catches a wrong/rotated BACKUP_ENCRYPT_KEYFILE or a corrupt archive
# BEFORE any DROP — a bad key found mid-reload would leave the DB dropped+empty).
# Only the first bytes are read (head closes the pipe early; guard pipefail).
for arch in "$JARVIS_ARCHIVE" "$LITELLM_ARCHIVE"; do
  set +e
  magic="$(decrypt_or_passthrough "$arch" 2>/dev/null | head -c 2 | od -An -tx1 | tr -d ' \n')"
  set -e
  if [ "$magic" != "1f8b" ]; then
    fail_before_destruction "backup archive $(basename "$arch") is unreadable (wrong encryption key or corrupt); nothing was changed"
  fi
done

# === STEP 3: maintenance ON + heartbeat ======================================
# Turn the stack to 503 for the whole restore, and re-touch the sentinel every
# 60s so a >30-min restore does not auto-expire (MAINTENANCE_MAX_AGE_S) mid-flight.
touch "$MAINTENANCE_SENTINEL"
MAIN_PID=$$
RESTORE_DEADLINE=$(( $(date +%s) + ${RESTORE_MAX_SECONDS:-3600} ))
(
  while true; do
    sleep 60
    touch "$MAINTENANCE_SENTINEL" 2>/dev/null || true
    if [ "$(date +%s)" -gt "$RESTORE_DEADLINE" ]; then
      # Deadline exceeded — abandon. If a DROP began (.destructive present) the
      # DB may be inconsistent: HOLD maintenance. If not, nothing was destroyed:
      # LIFT .maintenance. Then signal the main process so its EXIT trap writes
      # the single terminal status. The .restore_timeout marker tells _cleanup to
      # word the error as a timeout.
      : > "${TRIGGER_DIR}/.restore_timeout" 2>/dev/null || true
      if [ ! -f "$MAINTENANCE_DESTRUCTIVE" ]; then
        rm -f "$MAINTENANCE_SENTINEL" 2>/dev/null || true
      fi
      kill "$MAIN_PID" 2>/dev/null || true
      exit 0
    fi
  done
) &
HEARTBEAT_PID=$!

# === STEP 4: safety pre-backup (before ANY destruction) ======================
CURRENT_STEP="Safety backup"
PHASE="safety"
STEP_SAFETY="running"
write_status
# Skip the retention prune in the safety pre-backup: it would otherwise delete the
# very archive being restored when that archive is a `local` target older than
# RETENTION_DAYS, leaving STEP 5 to DROP the DB and then fail to reload it.
export BACKUP_SKIP_PRUNE=1
if /usr/local/bin/backup.sh; then :; else echo "[restore] WARN: safety backup.sh exited non-zero" >&2; fi
LAST_RUN="$(cat "${BACKUP_DIR}/.last_run.json" 2>/dev/null || true)"
SAFETY_SUCCEEDED="$(printf '%s' "$LAST_RUN" \
  | grep -oE '"succeeded"[[:space:]]*:[[:space:]]*(true|false)' | grep -oE 'true|false' | head -1 || true)"
SAFETY_BACKUP_TS="$(printf '%s' "$LAST_RUN" \
  | grep -oE '"timestamp"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' | grep -oE '[0-9]{8}_[0-9]{6}' | head -1 || true)"
if [ "$SAFETY_SUCCEEDED" != "true" ]; then
  STEP_SAFETY="failed"
  fail_before_destruction "safety backup failed; nothing was changed"
fi
STEP_SAFETY="done"
write_status

# Re-verify the resolved DB archives still exist AFTER the safety pre-backup and
# BEFORE the first DROP. The safety backup runs with BACKUP_SKIP_PRUNE=1 so it
# cannot prune them, but re-checking here is a cheap belt-and-braces guard: if a
# target archive vanished between STEP 1 resolution and now, fail before any
# destruction (fail_before_destruction lifts maintenance + records the error,
# no DROP) rather than DROP the DB and then fail to reload it.
for _arch in "$JARVIS_ARCHIVE" "$LITELLM_ARCHIVE"; do
  if [ ! -f "$_arch" ]; then
    fail_before_destruction "archive $(basename "$_arch") disappeared before the restore began (possibly pruned); nothing was changed"
  fi
done

# === STEP 5: restore the DBs — the ONLY destructive step =====================
CURRENT_STEP="Restoring database"
PHASE="reload-db"
STEP_DB="running"
write_status
if restore_one_db "$JARVIS_DB" "$JARVIS_ARCHIVE"; then
  STEP_DB="done"
else
  STEP_DB="failed"
  step5_fail
fi
write_status

CURRENT_STEP="Restoring API-key store"
PHASE="reload-litellm"
STEP_LITELLM="running"
write_status
if restore_one_db "$LITELLM_DB" "$LITELLM_ARCHIVE"; then
  STEP_LITELLM="done"
else
  STEP_LITELLM="failed"
  step5_fail
fi
write_status

# === STEP 7: Qdrant recover (best-effort, non-fatal, loud) ===================
# Vectors are rebuildable from Postgres by re-embedding, so a Qdrant failure is
# recorded as degraded and never fails the restore.
CURRENT_STEP="Restoring search index"
PHASE="qdrant"
if [ "${#QDRANT_SNAPS[@]}" -eq 0 ]; then
  STEP_QDRANT="skipped"
  write_status
else
  STEP_QDRANT="running"
  write_status
  QDRANT_API_KEY=""
  [ -r "$QDRANT_API_KEYFILE" ] && QDRANT_API_KEY="$(cat "$QDRANT_API_KEYFILE")"
  mkdir -p "$QDRANT_STAGING_DIR" 2>/dev/null || true
  QDRANT_OK=1
  for snap in "${QDRANT_SNAPS[@]}"; do
    base="$(basename "$snap")"
    col="$(printf '%s' "$base" | sed -E "s/^qdrant_(.+)_${TIMESTAMP}\.snapshot(\.enc)?$/\1/")"
    staged="${QDRANT_STAGING_DIR}/${col}.snapshot"
    if ! decrypt_or_passthrough "$snap" > "$staged" 2>/dev/null; then
      echo "[restore] WARN: could not stage Qdrant snapshot for '${col}'; continuing" >&2
      QDRANT_OK=0
      rm -f "$staged" 2>/dev/null || true
      continue
    fi
    if qdrant_http_body PUT "/collections/${col}/snapshots/recover" \
        "{\"location\":\"file://${QDRANT_STAGING_DIR}/${col}.snapshot\",\"priority\":\"snapshot\"}"; then
      echo "[restore] Qdrant collection '${col}' recovered" >&2
    else
      echo "[restore] WARN: Qdrant recover failed for '${col}'; vectors can be rebuilt by re-embedding" >&2
      QDRANT_OK=0
    fi
    rm -f "$staged" 2>/dev/null || true
  done
  if [ "$QDRANT_OK" -eq 1 ]; then STEP_QDRANT="done"; else STEP_QDRANT="degraded"; fi
  write_status
fi

# === STEP 8: cross-host secrets materialization + role rebind (inbox only) ===
# Same-host (local) restores never touch secrets: ./secrets is read-only and the
# live JARVIS_CONFIG_KEY already matches the restored Fernet rows — so this whole
# block is skipped and the local path is byte-for-byte unchanged.
#
# An off-host (inbox) restore runs on a FRESH host whose postgres cluster fixed the
# jarvis role's password at first-init from the NEW host's POSTGRES_PASSWORD. The
# restored DBs (pg_dump --no-owner --no-acl; cluster roles are not in the dump)
# leave that role untouched, but the app will read the OLD restored
# postgres_password.txt — so we rebind the live role to the restored password with
# ALTER ROLE. The secrets are decrypted to a WRITABLE staging dir (never the RO
# /secrets) purely to read that password; the EXIT trap shreds the operator key
# and removes this staging afterwards. The operator separately materializes the
# host ./secrets from the same archive (runbook) for config-key decryptability.
# Everything here runs AFTER the DB restore; the EXIT trap holds maintenance on any
# failure (DROP_STARTED=1).
if [ "$SOURCE" = "inbox" ]; then
  CURRENT_STEP="Restoring secrets"
  PHASE="secrets"
  write_status
  SECRETS_ARCHIVE=""
  for cand in "${ARCHIVE_DIR}/secrets_${TIMESTAMP}.tar.gz.enc" \
              "${ARCHIVE_DIR}/secrets_${TIMESTAMP}.tar.gz"; do
    if [ -f "$cand" ]; then SECRETS_ARCHIVE="$cand"; break; fi
  done
  if [ -z "$SECRETS_ARCHIVE" ]; then
    fail_after_restore "off-host restore: secrets archive secrets_${TIMESTAMP}.tar.gz[.enc] not found in the inbox; databases were restored — materialize ./secrets and rebind the postgres role manually per the runbook"
  fi
  purge_secrets_staging  # shred any leftover plaintext from a SIGKILLed prior run
  mkdir -p "$SECRETS_STAGING"
  chmod 700 "$SECRETS_STAGING"
  # The off-host secrets archive is OPERATOR-SUPPLIED and therefore untrusted:
  # materialize the decrypted stream to a temp file, enumerate its members, and
  # REJECT any absolute path or '..' traversal BEFORE extracting (a crafted member
  # like /etc/x or ../../x could otherwise escape the staging dir). Extract with
  # --no-same-owner --no-same-permissions so the archive cannot set ownership or
  # setuid/setgid bits. The staging dir is already chmod 700; the temp .tar and the
  # extracted plaintext are both shredded by purge_secrets_staging on exit. The
  # member check reads a captured var via a here-string (NOT a pipe): under
  # pipefail a `grep -q` that closes a pipe early on a huge crafted member list
  # would surface as SIGPIPE and be misread as "no unsafe member found".
  SECRETS_TAR_TMP="${SECRETS_STAGING}/.incoming.tar"
  if ! decrypt_or_passthrough "$SECRETS_ARCHIVE" > "$SECRETS_TAR_TMP" 2>/dev/null; then
    fail_after_restore "off-host restore: could not decrypt the secrets archive (wrong operator key or corrupt); databases were restored — see the runbook"
  fi
  SECRETS_TAR_MEMBERS="$(tar -tzf "$SECRETS_TAR_TMP" 2>/dev/null || true)"
  if grep -Eq '^/|\.\.|^[A-Za-z]:' <<<"$SECRETS_TAR_MEMBERS"; then
    fail_after_restore "off-host restore: the secrets archive contains an unsafe member path (absolute or '..' traversal); refusing to extract — see the runbook"
  fi
  if ! tar --no-same-owner --no-same-permissions -xzf "$SECRETS_TAR_TMP" -C "$SECRETS_STAGING" 2>/dev/null; then
    fail_after_restore "off-host restore: could not extract the secrets archive (corrupt); databases were restored — see the runbook"
  fi
  chmod -R go-rwx "$SECRETS_STAGING" 2>/dev/null || true
  OLD_PG_PW_FILE="${SECRETS_STAGING}/postgres_password.txt"
  if [ ! -s "$OLD_PG_PW_FILE" ]; then
    fail_after_restore "off-host restore: postgres_password.txt is missing from the restored secrets; cannot rebind the role — see the runbook"
  fi
  # Command substitution strips the trailing newline, matching how postgres and the
  # app read the secret. The password is a SQL string LITERAL (not a bind param), so
  # double every single quote before embedding it (a password containing ' would
  # otherwise break the statement or inject SQL).
  OLD_PG_PW="$(cat "$OLD_PG_PW_FILE")"
  if [ -z "$OLD_PG_PW" ]; then
    fail_after_restore "off-host restore: the restored postgres_password is empty; cannot rebind the role — see the runbook"
  fi
  OLD_PG_PW_ESC="${OLD_PG_PW//\'/\'\'}"
  if ! psql_admin "ALTER ROLE \"${PGUSER}\" WITH PASSWORD '${OLD_PG_PW_ESC}';" >/dev/null; then
    fail_after_restore "off-host restore: ALTER ROLE password rebind failed; databases were restored — rebind the postgres role manually per the runbook"
  fi
  write_status
fi

# === STEP 9: finishing up ====================================================
# A clean SAME-HOST restore lifts maintenance via the EXIT trap. A clean INBOX
# restore does NOT: the stack stays 503 until the operator materializes ./secrets
# and recreates the app containers (the runbook clears BOTH .maintenance and the
# durable .destructive sentinel as its final step — .destructive never auto-expires,
# so this clear is mandatory) — otherwise the app would serve 5xx auth errors
# instead of an honest 503.
CURRENT_STEP="Finishing up"
PHASE="finalize"
STEP_FINISH="running"
write_status
RESTORE_CLEAN=1
STATE="done"
if [ "$SOURCE" = "inbox" ] || [ "$RESTORE_OLDER" = "1" ]; then
  MANUAL_STEPS_REQUIRED=1
  PHASE="maintenance-held"
fi
STEP_FINISH="done"
FINISHED_AT="$(date -Iseconds)"
write_status
if [ "$SOURCE" = "inbox" ]; then
  echo "[restore] off-host restore complete: databases + vectors restored and the postgres role rebound. The stack stays in MAINTENANCE until you materialize the host ./secrets and recreate the app containers, then clear /backup-trigger/.maintenance and /backup-trigger/.destructive — see DEPLOYMENT.md." >&2
elif [ "$RESTORE_OLDER" = "1" ]; then
  echo "[restore] OLDER backup restored (schema ${MANIFEST_SCHEMA} < code ${CODE_MAX}). The stack stays in MAINTENANCE: recreate the app containers to run forward migrations, then clear /backup-trigger/.maintenance and /backup-trigger/.destructive — see DEPLOYMENT.md." >&2
fi
# The EXIT trap clears both .maintenance and .destructive on the clean same-host path
# + kills the heartbeat.
