#!/usr/bin/env bash
# backup.sh — JARVIS disaster-recovery backup sidecar
#
# What it backs up (JARVIS state lives in more than one place):
#   - PostgreSQL `jarvis` DB  → /backups/jarvis_<timestamp>.sql.gz[.enc]
#   - PostgreSQL `litellm` DB → /backups/litellm_<timestamp>.sql.gz[.enc]
#       (API keys, virtual keys, spend ledger; owned by the same superuser that
#        ran createdb, so the PGUSER dump has rights)
#   - Qdrant vector store     → /backups/qdrant_<collection>_<timestamp>.snapshot[.enc]
#       (one snapshot per collection via Qdrant's snapshot REST API; OPTIONAL
#        and non-fatal — if Qdrant is down the Postgres/secrets backups still
#        succeed)
#   - secrets/ directory      → /backups/secrets_<timestamp>.tar.gz[.enc]
#       (only the three keys coupled to restored database content)
#   - uploaded PDF files      → /backups/pdfs_<timestamp>.tar.gz[.enc]
#       (the numeric PDF objects referenced by rows in the Jarvis database)
#
# S3 upload (optional):
#   Set BACKUP_S3_BUCKET in .env to enable. Requires `aws` CLI in PATH.
#   If aws is not installed the script prints a notice and exits 0 (local
#   backup still succeeds). To add awscli to the backup sidecar image:
#     Alpine: apk add --no-cache aws-cli
#     pip:    pip install awscli
#
# Encryption (optional, at-rest):
#   Set BACKUP_ENCRYPT_KEYFILE to the path of a file containing the passphrase.
#   When set and the file is non-empty, archives are piped through
#   openssl enc -aes-256-cbc -pbkdf2 and saved with a .enc suffix.
#
# Decryption recipe:
#   openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -d -kfile "$BACKUP_ENCRYPT_KEYFILE" \
#       -in jarvis_<timestamp>.sql.gz.enc | gunzip > backup.sql
#   (secrets archive: same recipe, then `tar -xzf -`)
#
# Qdrant snapshot restore: copy the .snapshot file into the Qdrant snapshots
#   dir and PUT /collections/<name>/snapshots/recover — see DEPLOYMENT.md.
#
# Docker Compose: runs by default as the postgres-backup service (stop it to opt out).
# Env vars: BACKUP_S3_BUCKET, BACKUP_RETENTION_DAYS, BACKUP_INTERVAL_SECONDS,
#           BACKUP_ENCRYPT_KEYFILE, QDRANT_URL, LITELLM_DATABASE, SECRETS_DIR
#   — see .env.example for defaults.

set -euo pipefail

# --- Manifest authentication primitives --------------------------------------
# The manifest is plaintext metadata that anyone who can write BACKUP_DIR could edit
# (rewriting a sha256 to match a swapped archive), so it is signed and restore.sh
# verifies the signature before it trusts those checksums.
#
# derive_manifest_hmac_key computes HMAC with the PUBLIC domain label as the HMAC key
# over the SECRET key-file bytes as the message. That is deliberate, and it is not the
# textbook KDF direction: openssl offers no way to key on the secret without exposing it
# in the process list (`-hmac <secret>` and `-macopt hexkey:<secret>` both land in argv),
# and stdin is the only safe channel for the secret. The result still requires the key
# file to compute, so an attacker without it cannot forge a signature, and distinct
# labels yield independent sub-keys.
MANIFEST_HMAC_LABEL="jarvis-manifest-v1"

# `-r` is openssl's stable machine format "<hex> *stdin", so field 1 is always the bare
# hex regardless of the openssl version's default digest output format.
derive_manifest_hmac_key() {
  openssl dgst -sha256 -hmac "$MANIFEST_HMAC_LABEL" -r < "$ENC_KEYFILE" | cut -d' ' -f1
}

# promote_new_file <staged> <final> — atomically publish a staged file without
# ever replacing an existing restore-point member. Hard-link creation is the
# no-clobber commit; staged and final paths always share BACKUP_DIR/filesystem.
promote_new_file() {
  local staged="$1" final="$2"
  [ -e "$final" ] && return 1
  ln -- "$staged" "$final" 2>/dev/null || return 1
  rm -f -- "$staged"
}

# sign_manifest <manifest> — write <manifest>.hmac. Non-zero on any failure, leaving no
# partial signature behind (an absent signature is honest; a truncated one is not).
sign_manifest() {
  local manifest="$1" tmp="${1}.hmac.tmp"
  if ! openssl dgst -sha256 -mac HMAC -macopt "hexkey:$(derive_manifest_hmac_key)" -r < "$manifest" \
       | cut -d' ' -f1 > "$tmp" || [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    return 1
  fi
  if ! chmod 644 "$tmp" 2>/dev/null || ! promote_new_file "$tmp" "${manifest}.hmac"; then
    rm -f "$tmp"
    return 1
  fi
}

# backup_pdfs — archive the flat numeric PDF object store while holding the
# shared publish lock. Publishers take the same lock exclusively for their
# final rename, so the archive cannot observe a partially published file.
open_pdf_publish_lock_shared() {
  local lock="${PDF_STORAGE_DIR}/.publish.lock" path_id fd_id
  if [ -L "$lock" ]; then
    echo "FATAL: PDF publish lock is a symbolic link" >&2
    return 1
  fi
  if [ ! -e "$lock" ]; then
    (umask 022; set -C; : > "$lock") 2>/dev/null || [ -e "$lock" ] || return 1
  fi
  [ -f "$lock" ] && [ ! -L "$lock" ] || return 1
  path_id="$(stat -Lc '%d:%i' -- "$lock" 2>/dev/null || true)"
  [ -n "$path_id" ] || return 1
  exec 7<"$lock" || return 1
  fd_id="$(stat -Lc '%d:%i' -- /proc/self/fd/7 2>/dev/null || true)"
  if [ "$fd_id" != "$path_id" ] || [ -L "$lock" ]; then
    exec 7>&-
    return 1
  fi
  if ! flock -s 7; then
    exec 7>&-
    return 1
  fi
  path_id="$(stat -Lc '%d:%i' -- "$lock" 2>/dev/null || true)"
  fd_id="$(stat -Lc '%d:%i' -- /proc/self/fd/7 2>/dev/null || true)"
  if [ -L "$lock" ] || [ -z "$path_id" ] || [ "$fd_id" != "$path_id" ]; then
    flock -u 7 2>/dev/null || true
    exec 7>&-
    return 1
  fi
}

backup_pdfs() {
  local out tmp list candidate base
  if [ ! -d "$PDF_STORAGE_DIR" ] || [ -L "$PDF_STORAGE_DIR" ]; then
    echo "FATAL: PDF storage ${PDF_STORAGE_DIR} is missing or unsafe" >&2
    return 1
  fi
  open_pdf_publish_lock_shared || return 1

  while IFS= read -r -d '' candidate; do
    base="$(basename -- "$candidate")"
    printf '%s' "$base" | grep -Eq '^[0-9]+\.pdf$' || continue
    if [ ! -f "$candidate" ] || [ -L "$candidate" ]; then
      echo "FATAL: PDF object ${base} is not a safe regular file" >&2
      flock -u 7
      exec 7>&-
      return 1
    fi
  done < <(find "$PDF_STORAGE_DIR" -mindepth 1 -maxdepth 1 -name '*.pdf' -print0)

  if [ "$ENCRYPT" -eq 1 ]; then
    out="${BACKUP_DIR}/pdfs_${TIMESTAMP}.tar.gz.enc"
  else
    out="${BACKUP_DIR}/pdfs_${TIMESTAMP}.tar.gz"
  fi
  tmp="${out}.tmp"
  list="${out}.files.tmp"
  find "$PDF_STORAGE_DIR" -regextype posix-extended -mindepth 1 -maxdepth 1 \
    -type f -regex '.*/[0-9]+\.pdf' -printf '%f\0' | sort -z > "$list"
  tar -C "$PDF_STORAGE_DIR" --null --verbatim-files-from --no-recursion \
    -czf - -T "$list" | encrypt_or_passthrough > "$tmp"
  local st=("${PIPESTATUS[@]}")
  rm -f -- "$list"
  flock -u 7
  exec 7>&-
  if [ "${st[0]}" -ne 0 ] || [ "${st[1]}" -ne 0 ]; then
    rm -f -- "$tmp"
    echo "FATAL: PDF archive failed (tar=${st[0]} enc=${st[1]})" >&2
    return 1
  fi
  chmod 600 "$tmp"
  if ! promote_new_file "$tmp" "$out"; then
    rm -f -- "$tmp"
    echo "FATAL: refusing to overwrite existing PDF backup" >&2
    return 1
  fi
  printf '%s' "$out"
}

# The script's tests source it to exercise the two helpers above without taking a
# backup. Nothing above this line reads a secret, touches the DB, or writes a file.
if [ "${1:-}" = "--functions-only" ]; then
  # shellcheck disable=SC2317  # `return` succeeds when sourced and fails when
  # executed; the `exit` is the executed-path fallback, not dead code.
  return 0 2>/dev/null || exit 0
fi

# --- Optional S3 pull (off-host disaster recovery) ---------------------------
# When BACKUP_PULL_TS is set this run does NOT take a backup: it downloads the
# named timestamp's archive set (+ its manifest) from s3://$BACKUP_S3_BUCKET into
# BACKUP_PULL_DEST (the restore_inbox), then exits. The cross-host DR runbook uses
# it to fetch an off-site backup onto a fresh host before an `inbox` restore.
# Guarded by `aws` + a non-empty bucket; never fatal (a missing aws/bucket prints
# an honest one-line notice and returns). This branch only runs when BACKUP_PULL_TS
# is set, so the default scheduled-backup behavior below is unchanged.
pull_from_s3() {
  local ts="$1" dest="$2"
  if ! command -v aws >/dev/null 2>&1; then
    echo "[$(date -Iseconds)] S3 pull requested but the aws CLI is not installed; install awscli or copy the archive set into ${dest} manually" >&2
    return 0
  fi
  if [ -z "${BACKUP_S3_BUCKET:-}" ]; then
    echo "[$(date -Iseconds)] S3 pull requested but BACKUP_S3_BUCKET is empty; set it to the off-site bucket holding the archive set" >&2
    return 0
  fi
  mkdir -p "$dest"
  # cp --recursive with include/exclude filters fetches just this timestamp's
  # archive set + manifest without enumerating the variable .enc / qdrant-collection
  # filenames. The keys are flat basenames in the bucket.
  aws s3 cp "s3://${BACKUP_S3_BUCKET}/" "$dest" --recursive \
    --exclude "*" --include "*_${ts}.*" --include "manifest_${ts}.json" \
    --include "manifest_${ts}.json.hmac"
  echo "[$(date -Iseconds)] Pulled backup ${ts} from s3://${BACKUP_S3_BUCKET}/ into ${dest}" >&2
}

if [ -n "${BACKUP_PULL_TS:-}" ]; then
  pull_from_s3 "$BACKUP_PULL_TS" "${BACKUP_PULL_DEST:-/restore-inbox}"
  exit 0
fi

# Read PGPASSWORD from Docker Secret — required; fail fast if missing. The path
# override is used by isolated recovery tests and custom secret mounts; Compose
# keeps the default.
POSTGRES_PASSWORD_FILE="${POSTGRES_PASSWORD_FILE:-/run/secrets/postgres_password}"
if [ ! -r "$POSTGRES_PASSWORD_FILE" ]; then
    echo "FATAL: cannot read ${POSTGRES_PASSWORD_FILE}" >&2
    exit 1
fi
PGPASSWORD="$(cat "$POSTGRES_PASSWORD_FILE")"
export PGPASSWORD

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
ENVIRONMENT="${ENVIRONMENT:-development}"
# When set (e.g. by restore.sh's safety pre-backup) the retention prune at the
# tail is skipped, so a pre-restore safety backup can never delete the very
# archive being restored (a `local` archive older than RETENTION_DAYS).
BACKUP_SKIP_PRUNE="${BACKUP_SKIP_PRUNE:-}"
# When set (by restore.sh's safety pre-backup) the maintenance skip-guard is
# bypassed: the restore itself holds .maintenance, so its own safety snapshot
# must run. Every producer still takes the mutex below.
BACKUP_FORCE="${BACKUP_FORCE:-}"
ENC_KEYFILE="${BACKUP_ENCRYPT_KEYFILE:-}"
LITELLM_DATABASE="${LITELLM_DATABASE:-litellm}"
QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"
QDRANT_API_KEYFILE="${QDRANT_API_KEYFILE:-/run/secrets/qdrant_api_key}"
SECRETS_DIR="${SECRETS_DIR:-/secrets}"
PDF_STORAGE_DIR="${PDF_STORAGE_DIR:-/pdf-storage}"
# The sidecar's rw mount of the host ./secrets. It holds the out-of-band marker that
# tells restore.sh a signed manifest is now mandatory: keeping that marker OUT of
# BACKUP_DIR means an attacker who can only write BACKUP_DIR cannot remove it to
# downgrade a later restore back to unsigned.
HOST_SECRETS_DIR="${HOST_SECRETS_DIR:-/host-secrets}"
MANIFEST_HMAC_MARKER="${HOST_SECRETS_DIR}/manifest-hmac-required"
TIMESTAMP=""
RUN_ID=""

# When a backup encryption key is configured archives get a .enc suffix and are
# piped through openssl; otherwise they are written in the clear.
if [ -n "$ENC_KEYFILE" ] && [ -s "$ENC_KEYFILE" ]; then
  ENCRYPT=1
else
  ENCRYPT=0
fi

mkdir -p "$BACKUP_DIR"

# On-demand trigger: the WebUI Backup panel writes /backup-trigger/.backup_now to
# request an immediate run. A run claims the current flag by atomic rename after
# taking the backup mutex. A new flag written after that claim remains at the
# public path for the next run.
TRIGGER_DIR="${BACKUP_TRIGGER_DIR:-/backup-trigger}"
TRIGGER_FILE="${TRIGGER_DIR}/.backup_now"
LOCK_DIR="${BACKUP_DIR}/.lifecycle"
BACKUP_LOCK="${LOCK_DIR}/backup.lock"
UPDATE_LOCK="${LOCK_DIR}/update.lock"
LEGACY_BACKUP_LOCK="${TRIGGER_DIR}/.backup.lock"
# Update transactions put their correlation ID in the trigger. Empty legacy/UI
# flag-files remain supported and receive a generated ID after the mutex is held.
TRIGGER_RUN_ID=""
CLAIMED_TRIGGER_FILE=""

# Move the trigger to a path owned by this process. The rename is atomic within
# TRIGGER_DIR, so writers can immediately publish the next request at
# TRIGGER_FILE without it being removed by this run.
claim_backup_trigger() {
  local claimed
  CLAIMED_TRIGGER_FILE=""
  if [ ! -e "$TRIGGER_FILE" ] && [ ! -L "$TRIGGER_FILE" ]; then
    return 0
  fi
  [ -f "$TRIGGER_FILE" ] && [ ! -L "$TRIGGER_FILE" ] || return 1
  claimed="${TRIGGER_FILE}.claimed.${BASHPID}"
  [ ! -e "$claimed" ] && [ ! -L "$claimed" ] || return 1
  mv -T -- "$TRIGGER_FILE" "$claimed" || return 1
  CLAIMED_TRIGGER_FILE="$claimed"
}

# Read and remove only the private claim. Never touch TRIGGER_FILE here: it may
# already be a request written for the next run.
consume_claimed_backup_trigger() {
  local claimed value
  TRIGGER_RUN_ID=""
  claimed="$CLAIMED_TRIGGER_FILE"
  [ -n "$claimed" ] || return 0
  if [ ! -f "$claimed" ] || [ -L "$claimed" ]; then
    rm -f -- "$claimed" 2>/dev/null || true
    CLAIMED_TRIGGER_FILE=""
    return 1
  fi
  value="$(tr -d '\r\n' < "$claimed")" || return 1
  rm -f -- "$claimed" || return 1
  CLAIMED_TRIGGER_FILE=""
  TRIGGER_RUN_ID="$value"
}

prepare_lock_dir() {
  [ ! -L "$LOCK_DIR" ] || return 1
  mkdir -p "$LOCK_DIR" || return 1
  [ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] || return 1
  chmod 700 "$LOCK_DIR" || return 1
}

# Coordinate once with a backup process started before the v1.2 private-lock
# migration. This opens an existing regular legacy inode read-only; it never
# creates or truncates a path controlled by the application container.
open_legacy_backup_lock() {
  local opened path_identity
  if [ ! -e "$LEGACY_BACKUP_LOCK" ] && [ ! -L "$LEGACY_BACKUP_LOCK" ]; then
    return 1
  fi
  if [ -L "$LEGACY_BACKUP_LOCK" ] || [ ! -f "$LEGACY_BACKUP_LOCK" ]; then
    echo "[backup] WARNING: unsafe legacy backup lock path" >&2
    return 2
  fi
  if ! exec 6<"$LEGACY_BACKUP_LOCK"; then
    echo "[backup] WARNING: could not inspect legacy backup lock" >&2
    return 2
  fi
  opened="$(stat -Lc '%d:%i' "/proc/$$/fd/6" 2>/dev/null || true)"
  path_identity="$(stat -Lc '%d:%i' "$LEGACY_BACKUP_LOCK" 2>/dev/null || true)"
  if [ -z "$opened" ] || [ "$opened" != "$path_identity" ] \
     || [ -L "$LEGACY_BACKUP_LOCK" ]; then
    exec 6>&-
    echo "[backup] WARNING: legacy backup lock changed while it was inspected" >&2
    return 2
  fi
  return 0
}

restore_point_exists() {
  local ts="$1"
  find "$BACKUP_DIR" -maxdepth 1 -type f \( \
    -name "*_${ts}.*" -o -name "manifest_${ts}.json" -o -name "manifest_${ts}.json.hmac" \
  \) -print -quit 2>/dev/null | grep -q .
}

# Called only while holding the private backup mutex. It advances by one second across any
# existing identity and validates/generates the signed correlation ID.
allocate_backup_identity() {
  local epoch attempts=0 requested
  epoch="$(date +%s)"
  while [ "$attempts" -lt 86400 ]; do
    TIMESTAMP="$(date -d "@${epoch}" +%Y%m%d_%H%M%S)"
    restore_point_exists "$TIMESTAMP" || break
    epoch=$((epoch + 1)); attempts=$((attempts + 1))
  done
  [ "$attempts" -lt 86400 ] || return 1
  requested="${BACKUP_RUN_ID:-${TRIGGER_RUN_ID:-}}"
  if [ -n "$requested" ]; then
    printf '%s' "$requested" | grep -Eq '^[0-9a-f]{32}$' || return 1
    RUN_ID="$requested"
  else
    RUN_ID="$(openssl rand -hex 16 2>/dev/null || true)"
    printf '%s' "$RUN_ID" | grep -Eq '^[0-9a-f]{32}$' || return 1
  fi
}

# encrypt_or_passthrough — read stdin, write encrypted (when a key is set) or
# verbatim to stdout. Used as the final transform stage of a backup pipeline.
encrypt_or_passthrough() {
  if [ "$ENCRYPT" -eq 1 ]; then
    openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -kfile "$ENC_KEYFILE"
  else
    cat
  fi
}

# Per-store outcome tracking so a FAILED run is visible to /status — not just
# "no new archive" (which reads as "no recent backup"). The trap fires on EVERY
# exit path, so a FATAL pg_dump failure (set -e) still records jarvis=failed.
# Primary DBs start failed (a crash before they succeed must read as failed);
# best-effort stores start skipped (no attempt yet).
ATTEMPTED_AT="$(date -Iseconds)"
JARVIS_STATE="failed"
LITELLM_STATE="failed"
PDFS_STATE="failed"
SECRETS_STATE="skipped"
QDRANT_STATE="skipped"
MANIFEST_STATE="failed"
if [ "$ENCRYPT" -eq 1 ]; then
  MANIFEST_SIGNATURE_STATE="failed"
else
  MANIFEST_SIGNATURE_STATE="skipped"
fi

# write_last_run — emit ${BACKUP_DIR}/.last_run.json (a dotfile, NOT an archive,
# so the router's allowlist/globs never match it).
write_last_run() {
  [ "${SKIP_LAST_RUN_WRITE:-0}" = "1" ] && return 0
  local succeeded="false"
  local secrets_complete="false"
  if [ "$SECRETS_STATE" = "ok" ] \
     || { [ "$SECRETS_STATE" = "skipped" ] \
          && [ "$ENCRYPT" -eq 0 ] \
          && [ "${ENVIRONMENT:-development}" != "production" ]; }; then
    secrets_complete="true"
  fi
  local signature_complete="false"
  if { [ "$ENCRYPT" -eq 0 ] && [ "$MANIFEST_SIGNATURE_STATE" = "skipped" ]; } \
     || { [ "$ENCRYPT" -eq 1 ] && [ "$MANIFEST_SIGNATURE_STATE" = "ok" ]; }; then
    signature_complete="true"
  fi
  if [ "$JARVIS_STATE" = "ok" ] \
     && [ "$LITELLM_STATE" = "ok" ] \
     && [ "$PDFS_STATE" = "ok" ] \
     && [ "$secrets_complete" = "true" ] \
     && [ "$MANIFEST_STATE" = "ok" ] \
     && [ "$signature_complete" = "true" ]; then
    succeeded="true"
  fi
  local enc="false"
  [ "$ENCRYPT" -eq 1 ] && enc="true"
  # skipped_maintenance distinguishes a run that intentionally stood down for an
  # in-flight restore (succeeded stays false, but it is NOT a failure) from a real
  # backup failure, so /status can word the two differently.
  local skipped="false"
  [ "${SKIPPED_MAINTENANCE:-0}" = "1" ] && skipped="true"
  local lr_tmp="${BACKUP_DIR}/.last_run.json.tmp"
  cat > "$lr_tmp" <<JSON
{"attempted_at":"${ATTEMPTED_AT}","timestamp":"${TIMESTAMP}","run_id":"${RUN_ID}","succeeded":${succeeded},"encrypted":${enc},"skipped_maintenance":${skipped},"retention_days":${RETENTION_DAYS},"stores":{"jarvis":"${JARVIS_STATE}","litellm":"${LITELLM_STATE}","pdfs":"${PDFS_STATE}","secrets":"${SECRETS_STATE}","qdrant":"${QDRANT_STATE}","manifest":"${MANIFEST_STATE}","manifest_signature":"${MANIFEST_SIGNATURE_STATE}"}}
JSON
  mv -f "$lr_tmp" "${BACKUP_DIR}/.last_run.json"
}
trap write_last_run EXIT

# --- Maintenance skip-guard + single-run mutex -------------------------------
# A restore (restore.sh) raises the .maintenance / .destructive sentinels for its
# whole run. A SCHEDULED or on-demand backup that fires in that window must NOT run
# — dumping mid drop-swap would capture an inconsistent DB — so it SKIPS and tags
# the skip in .last_run.json (the EXIT trap still writes it, so /status can show
# "skipped for restore", distinct from a real failure). The restore's OWN safety
# pre-backup sets BACKUP_FORCE=1 to bypass this: it needs a snapshot BEFORE the
# destruction and must never treat the restore's own sentinel as a reason to skip.
# Sentinel paths + max-age mirror jarvis_common.maintenance.maintenance_active so
# both gates agree; keying on LIVE sentinel presence means a cleared sentinel
# resumes scheduled backups immediately (no stale-status wedge).
MAINTENANCE_SENTINEL="${MAINTENANCE_SENTINEL:-${TRIGGER_DIR}/.maintenance}"
MAINTENANCE_DESTRUCTIVE="${MAINTENANCE_DESTRUCTIVE_SENTINEL:-${TRIGGER_DIR}/.destructive}"
CONFIG_KEY_ROTATION_SENTINEL="${CONFIG_KEY_ROTATION_SENTINEL:-${LOCK_DIR}/rotation.guard}"
MAINTENANCE_MAX_AGE_S="${MAINTENANCE_MAX_AGE_S:-1800}"
SKIPPED_MAINTENANCE=0

# maintenance_active — mirror of jarvis_common.maintenance.maintenance_active:
# .destructive is active regardless of age; .maintenance is active only while
# fresher than MAINTENANCE_MAX_AGE_S, so a crashed restore's stale soft sentinel
# cannot wedge scheduled backups off forever. Never fatal under set -e.
maintenance_active() {
  [ -e "$MAINTENANCE_DESTRUCTIVE" ] && return 0
  [ -e "$MAINTENANCE_SENTINEL" ] || return 1
  local now mtime
  now="$(date +%s)"
  mtime="$(stat -c %Y "$MAINTENANCE_SENTINEL" 2>/dev/null || echo 0)"
  [ "$(( now - mtime ))" -le "$MAINTENANCE_MAX_AGE_S" ]
}

# A config-key rotation changes encrypted DB rows and the key archived from
# /secrets as one unit. Its sentinel never ages out and BACKUP_FORCE may not
# bypass it: after a crash, a restore safety backup would otherwise preserve the
# same mixed ciphertext/key state that ordinary backups are forbidden to create.
config_key_rotation_active() { [ -e "$CONFIG_KEY_ROTATION_SENTINEL" ]; }

# Serialize the maintenance decision and its .last_run result with every archive
# producer. Otherwise a run that sees .maintenance can overwrite the active
# producer's eventual status before it ever attempts the shared lock.
mkdir -p "$TRIGGER_DIR"
if ! prepare_lock_dir || [ -L "$BACKUP_LOCK" ]; then
  echo "[backup] FATAL: private backup mutex path is unsafe" >&2
  exit 1
fi
exec 9>>"$BACKUP_LOCK"
if ! flock -n 9; then
  SKIP_LAST_RUN_WRITE=1
  echo "[backup] another backup is already running; skipping" >&2
  exit 0
fi
legacy_lock_state=0
open_legacy_backup_lock || legacy_lock_state=$?
if [ "$legacy_lock_state" -eq 2 ]; then
  echo "[backup] FATAL: unsafe legacy backup lock; confirm no pre-upgrade backup is running, remove the trigger-volume lock path, and retry" >&2
  exit 1
fi
if [ "$legacy_lock_state" -eq 0 ] && ! flock -n 6; then
  SKIP_LAST_RUN_WRITE=1
  echo "[backup] a pre-upgrade backup is still running; skipping" >&2
  exit 0
fi

if config_key_rotation_active \
   || { [ -z "$BACKUP_FORCE" ] && maintenance_active; }; then
  SKIPPED_MAINTENANCE=1
  # A stand-down is not an attempt, let alone a failure: keep the primary
  # stores out of their "failed" startup default (mirrors how SECRETS_STATE/
  # QDRANT_STATE already default to "skipped") so the EXIT trap's
  # write_last_run does not misreport this run's stores as failed.
  JARVIS_STATE="skipped"
  LITELLM_STATE="skipped"
  PDFS_STATE="skipped"
  echo "[backup] skipped: a restore holds the maintenance sentinel" >&2
  exit 0
fi

# Production backups are an all-encrypted contract, not merely an encrypted-
# secrets contract. Refuse before allocating an identity or invoking pg_dump so
# a missing/empty key can never publish plaintext database archives. This runs
# under the backup mutex, after the maintenance stand-down decision, so its
# failed .last_run record cannot race an active producer.
if [ "$ENVIRONMENT" = "production" ] && [ "$ENCRYPT" -ne 1 ]; then
  echo "FATAL: BACKUP_ENCRYPT_KEYFILE is unset or empty in production — refusing to write plaintext backup archives. Set it to a non-empty key file before running backups." >&2
  exit 1
fi

# Every producer uses one mutex. BACKUP_FORCE bypasses only the restore's own
# maintenance sentinel; it may never race another archive producer.
if ! claim_backup_trigger || ! consume_claimed_backup_trigger; then
  echo "[backup] FATAL: could not safely claim the on-demand backup request" >&2
  exit 1
fi
if ! allocate_backup_identity; then
  echo "[backup] FATAL: could not allocate a unique backup timestamp and strict run ID" >&2
  exit 1
fi

# dump_db <db-name> <archive-prefix>
#   pg_dump <db> | gzip [| openssl] → /backups/<prefix>_<ts>.sql.gz[.enc]
#   Stages to <final>.tmp and promotes ONLY when every pipeline stage exited 0
#   (checked via PIPESTATUS), so a failed pg_dump — which would otherwise leave
#   a tiny but well-formed empty-gzip archive — never masquerades as a backup.
#   Prints the final path on stdout (logs to stderr) for the caller to capture.
dump_db() {
  local db="$1" prefix="$2" out
  if [ "$ENCRYPT" -eq 1 ]; then
    out="${BACKUP_DIR}/${prefix}_${TIMESTAMP}.sql.gz.enc"
  else
    out="${BACKUP_DIR}/${prefix}_${TIMESTAMP}.sql.gz"
  fi
  local tmp="${out}.tmp"
  pg_dump -h "${PGHOST:-postgres}" -U "${PGUSER:-jarvis}" -d "$db" --no-owner --no-acl \
    | gzip \
    | encrypt_or_passthrough > "$tmp"
  local st=("${PIPESTATUS[@]}")
  if [ "${st[0]}" -ne 0 ] || [ "${st[1]}" -ne 0 ] || [ "${st[2]}" -ne 0 ]; then
    rm -f "$tmp"
    echo "FATAL: dump of '$db' failed (pg_dump=${st[0]} gzip=${st[1]} enc=${st[2]})" >&2
    return 1
  fi
  chmod 600 "$tmp"
  if ! promote_new_file "$tmp" "$out"; then
    rm -f "$tmp"
    echo "FATAL: refusing to overwrite existing backup member '$out'" >&2
    return 1
  fi
  # Log to stderr so command substitution captures only the filename below.
  echo "[$(date -Iseconds)] Backup saved to $out ($(du -h "$out" | cut -f1))" >&2
  printf '%s' "$out"
}

echo "[$(date -Iseconds)] Starting backup..."
BACKUP_ARCHIVES=()

# --- PostgreSQL: jarvis (primary) + litellm (API keys / virtual keys / spend) -
JARVIS_BACKUP_FILE="$(dump_db "${PGDATABASE:-jarvis}" jarvis)"
JARVIS_STATE="ok"
BACKUP_ARCHIVES+=("$JARVIS_BACKUP_FILE")
LITELLM_BACKUP_FILE="$(dump_db "$LITELLM_DATABASE" litellm)"
LITELLM_STATE="ok"
BACKUP_ARCHIVES+=("$LITELLM_BACKUP_FILE")

# The database rows refer to these files. The dump is taken first, then the PDF
# archive: concurrent publishers write the file before committing its row, so a
# race can add only an unreferenced file, never omit a referenced one.
PDFS_BACKUP_FILE="$(backup_pdfs)"
PDFS_STATE="ok"
BACKUP_ARCHIVES+=("$PDFS_BACKUP_FILE")
echo "[$(date -Iseconds)] Backup saved to $PDFS_BACKUP_FILE ($(du -h "$PDFS_BACKUP_FILE" | cut -f1))"

# --- secrets/ directory ------------------------------------------------------
# Only keys coupled to restored data cross hosts. Service credentials belong to
# the target host and are deliberately excluded.
# When no key is set: refuse outright in production (plaintext secrets on disk
# is unacceptable); silently skip in non-production with a clear warning.
SECRETS_BACKUP_FILE=""
if [ -d "$SECRETS_DIR" ]; then
  if [ "$ENCRYPT" -eq 1 ]; then
    SECRETS_BACKUP_FILE="${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz.enc"
    secrets_tmp="${SECRETS_BACKUP_FILE}.tmp"
    DATA_KEY_FILES=(jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt)
    for data_key in "${DATA_KEY_FILES[@]}"; do
      if [ ! -f "${SECRETS_DIR}/${data_key}" ] || [ -L "${SECRETS_DIR}/${data_key}" ] \
         || [ ! -s "${SECRETS_DIR}/${data_key}" ]; then
        rm -f "$secrets_tmp"
        SECRETS_STATE="failed"
        echo "FATAL: required data key ${data_key} is missing, empty, or unsafe" >&2
        exit 1
      fi
    done
    tar -czf - -C "$SECRETS_DIR" -- "${DATA_KEY_FILES[@]}" \
      | encrypt_or_passthrough > "$secrets_tmp"
    secrets_st=("${PIPESTATUS[@]}")
    if [ "${secrets_st[0]}" -ne 0 ] || [ "${secrets_st[1]}" -ne 0 ]; then
      rm -f "$secrets_tmp"
      SECRETS_STATE="failed"
      echo "FATAL: secrets archive failed (tar=${secrets_st[0]} enc=${secrets_st[1]})" >&2
      exit 1
    fi
    chmod 600 "$secrets_tmp"
    if ! promote_new_file "$secrets_tmp" "$SECRETS_BACKUP_FILE"; then
      rm -f "$secrets_tmp"
      SECRETS_STATE="failed"
      echo "FATAL: refusing to overwrite existing secrets backup" >&2
      exit 1
    fi
    SECRETS_STATE="ok"
    BACKUP_ARCHIVES+=("$SECRETS_BACKUP_FILE")
    echo "[$(date -Iseconds)] Backup saved to $SECRETS_BACKUP_FILE ($(du -h "$SECRETS_BACKUP_FILE" | cut -f1))"
  elif [ "$ENVIRONMENT" = "production" ]; then
    echo "FATAL: BACKUP_ENCRYPT_KEYFILE is unset in production — refusing to write a plaintext secrets archive. Set BACKUP_ENCRYPT_KEYFILE to a non-empty key file before running backups." >&2
    exit 1
  else
    SECRETS_STATE="skipped"
    echo "[$(date -Iseconds)] WARNING: BACKUP_ENCRYPT_KEYFILE is unset — secrets archive skipped (plaintext keys will NOT be written to disk). Set BACKUP_ENCRYPT_KEYFILE to include secrets in the backup." >&2
  fi
else
  echo "[$(date -Iseconds)] secrets dir $SECRETS_DIR not mounted; skipping secrets backup"
fi

# --- Qdrant vector store (OPTIONAL / non-fatal) ------------------------------
# One snapshot per collection via Qdrant's snapshot REST API. A down or
# unreachable Qdrant must not abort the Postgres/secrets backups above, so the
# whole block runs best-effort: any failure is logged and execution continues.
QDRANT_API_KEY=""
[ -r "$QDRANT_API_KEYFILE" ] && QDRANT_API_KEY="$(cat "$QDRANT_API_KEYFILE")"

# qdrant_http <METHOD> <path> [outfile]
#   Minimal HTTP client using perl's core HTTP::Tiny (the postgres image has no
#   curl/wget). Streams the body to <outfile> if given, else stdout. Returns
#   non-zero on transport error or non-2xx status.
qdrant_http() {
  QDRANT_URL="$QDRANT_URL" QDRANT_API_KEY="$QDRANT_API_KEY" \
  perl -MHTTP::Tiny -e '
    my ($method, $path, $out) = @ARGV;
    my %h = ("api-key" => $ENV{QDRANT_API_KEY}) if length $ENV{QDRANT_API_KEY};
    my %opts = ( headers => \%h );
    my $fh;
    if (defined $out && length $out) {
      # Stream the (potentially large) snapshot straight to disk instead of
      # buffering the whole body in memory — the sidecar runs under a 256m
      # mem_limit and a real collection snapshot would OOM-kill a buffered read.
      open($fh, ">", $out) or exit 1;
      binmode $fh;
      $opts{data_callback} = sub { my ($chunk) = @_; print $fh $chunk; };
    }
    my $res = HTTP::Tiny->new(timeout => 30)->request(
      $method, $ENV{QDRANT_URL} . $path, \%opts);
    if ($fh) { close $fh; }
    if (!$res->{success}) { unlink $out if $fh; exit 1; }
    if (!$fh) { print $res->{content}; }
  ' "$@"
}

if collections_json="$(qdrant_http GET /collections 2>/dev/null)"; then
  # Extract collection names from {"result":{"collections":[{"name":"..."}]}}.
  collections="$(printf '%s' "$collections_json" \
    | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
  if [ -z "$collections" ]; then
    echo "[$(date -Iseconds)] Qdrant has no collections to snapshot"
  fi
  for col in $collections; do
    if ! printf '%s' "$col" | grep -Eq '^[A-Za-z0-9_-]+$'; then
      QDRANT_STATE="failed"
      echo "[$(date -Iseconds)] WARNING: Qdrant collection name cannot be represented safely in a backup filename; skipping '$col'" >&2
      continue
    fi
    # POST /collections/<col>/snapshots → {"result":{"name":"<snapshot>"}}
    if create_json="$(qdrant_http POST "/collections/${col}/snapshots" 2>/dev/null)"; then
      snap="$(printf '%s' "$create_json" \
        | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 \
        | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
      if [ -n "$snap" ]; then
        # Stream the snapshot straight to disk (memory-safe under mem_limit), then
        # stream that raw file through encrypt_or_passthrough so the on-disk vector
        # snapshot is at-rest encrypted (.enc) like the DB/secrets archives — it is
        # not a separate plane of plaintext. openssl reads stdin incrementally, so
        # the encrypt stage stays within the 256m mem_limit too.
        qsnap_raw="${BACKUP_DIR}/qdrant_${col}_${TIMESTAMP}.snapshot.raw"
        if [ "$ENCRYPT" -eq 1 ]; then
          qsnap_out="${BACKUP_DIR}/qdrant_${col}_${TIMESTAMP}.snapshot.enc"
        else
          qsnap_out="${BACKUP_DIR}/qdrant_${col}_${TIMESTAMP}.snapshot"
        fi
        qsnap_tmp="${qsnap_out}.tmp"
        if qdrant_http GET "/collections/${col}/snapshots/${snap}" "$qsnap_raw"; then
          if encrypt_or_passthrough < "$qsnap_raw" > "$qsnap_tmp"; then
            chmod 600 "$qsnap_tmp"
            if ! promote_new_file "$qsnap_tmp" "$qsnap_out"; then
              rm -f "$qsnap_raw" "$qsnap_tmp"
              QDRANT_STATE="failed"
              echo "[$(date -Iseconds)] WARNING: refusing to overwrite Qdrant snapshot for '$col'; continuing" >&2
              continue
            fi
            rm -f "$qsnap_raw"
            BACKUP_ARCHIVES+=("$qsnap_out")
            echo "[$(date -Iseconds)] Qdrant snapshot saved to $qsnap_out ($(du -h "$qsnap_out" | cut -f1))"
          else
            rm -f "$qsnap_raw" "$qsnap_tmp"
            QDRANT_STATE="failed"
            echo "[$(date -Iseconds)] WARNING: failed to encrypt Qdrant snapshot for '$col'; continuing" >&2
          fi
        else
          rm -f "$qsnap_raw"
          QDRANT_STATE="failed"
          echo "[$(date -Iseconds)] WARNING: failed to download Qdrant snapshot for '$col'; continuing" >&2
        fi
      else
        QDRANT_STATE="failed"
        echo "[$(date -Iseconds)] WARNING: could not parse Qdrant snapshot name for '$col'; continuing" >&2
      fi
    else
      QDRANT_STATE="failed"
      echo "[$(date -Iseconds)] WARNING: failed to create Qdrant snapshot for '$col'; continuing" >&2
    fi
  done
  # No snapshot failed and at least one collection was processed → ok. No
  # collections (nothing to snapshot) or unreachable Qdrant stays skipped.
  if [ "$QDRANT_STATE" = "skipped" ] && [ -n "$collections" ]; then
    QDRANT_STATE="ok"
  fi
else
  echo "[$(date -Iseconds)] Qdrant unreachable at $QDRANT_URL; skipping vector snapshot (Postgres/secrets backups unaffected)" >&2
fi

# --- Backup manifest ---------------------------------------------------------
# discard_current_backup — delete only the exact timestamp being finalized, so
# a failed manifest or signature cannot leave an apparently complete restore point.
discard_current_backup() {
  local f
  for f in "${BACKUP_ARCHIVES[@]}"; do rm -f -- "$f"; done
  rm -f -- "${BACKUP_DIR}/manifest_${TIMESTAMP}.json" \
    "${BACKUP_DIR}/manifest_${TIMESTAMP}.json.tmp" \
    "${BACKUP_DIR}/manifest_${TIMESTAMP}.json.hmac" \
    "${BACKUP_DIR}/manifest_${TIMESTAMP}.json.hmac.tmp"
}

# write_manifest — emit ${BACKUP_DIR}/manifest_${TIMESTAMP}.json listing every
# archive this run produced (filename, sha256, size) plus app_version and the
# applied schema_version, so the restore UI can show per-restore-point version
# compatibility. PLAINTEXT METADATA ONLY (filenames, hex digests, integers — no
# secrets); it is not matched by the router's archive globs, so it is never
# listed or downloaded as a backup.
write_manifest() {
  local schema_version app_version created_at manifest manifest_tmp
  local first f base sum size
  declare -A seen=()
  schema_version="$(psql -h "${PGHOST:-postgres}" -U "${PGUSER:-jarvis}" \
    -d "${PGDATABASE:-jarvis}" -tAc 'SELECT COALESCE(MAX(version),0) FROM schema_migrations' \
    2>/dev/null || echo 0)"
  case "$schema_version" in
    ''|*[!0-9]*) schema_version=0 ;;
  esac
  app_version="${JARVIS_VERSION:-unknown}"
  created_at="$(date -Iseconds)"
  manifest="${BACKUP_DIR}/manifest_${TIMESTAMP}.json"
  manifest_tmp="${manifest}.tmp"
  if ! (
    printf '{"timestamp":"%s","run_id":"%s","app_version":"%s","schema_version":%s,"created_at":"%s","archives":[' \
      "$TIMESTAMP" "$RUN_ID" "$app_version" "$schema_version" "$created_at" || exit 1
    first=1
    for f in "${BACKUP_ARCHIVES[@]}"; do
      [ -f "$f" ] || exit 1
      case "$f" in "${BACKUP_DIR}"/*) ;; *) exit 1 ;; esac
      base="$(basename "$f")"
      case "$base" in
        "jarvis_${TIMESTAMP}.sql.gz"|"jarvis_${TIMESTAMP}.sql.gz.enc"|\
        "litellm_${TIMESTAMP}.sql.gz"|"litellm_${TIMESTAMP}.sql.gz.enc"|\
        "pdfs_${TIMESTAMP}.tar.gz"|"pdfs_${TIMESTAMP}.tar.gz.enc"|\
        "secrets_${TIMESTAMP}.tar.gz"|"secrets_${TIMESTAMP}.tar.gz.enc"|\
        qdrant_*_"${TIMESTAMP}.snapshot"|qdrant_*_"${TIMESTAMP}.snapshot.enc") ;;
        *) exit 1 ;;
      esac
      [ -z "${seen[$base]+x}" ] || exit 1
      seen[$base]=1
      sum="$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)" || exit 1
      printf '%s' "$sum" | grep -Eq '^[0-9a-f]{64}$' || exit 1
      size="$(stat -c%s "$f" 2>/dev/null)" || exit 1
      case "$size" in
        ''|*[!0-9]*) exit 1 ;;
      esac
      [ "$first" -eq 1 ] || printf ',' || exit 1
      first=0
      printf '{"filename":"%s","sha256":"%s","size_bytes":%s}' "$base" "$sum" "$size" || exit 1
    done
    printf ']}' || exit 1
  ) > "$manifest_tmp" 2>/dev/null; then
    echo "[$(date -Iseconds)] FATAL: manifest write or archive hash failed" >&2
    rm -f "$manifest_tmp"
    return 1
  fi
  if ! chmod 644 "$manifest_tmp" 2>/dev/null \
     || ! promote_new_file "$manifest_tmp" "$manifest"; then
    echo "[$(date -Iseconds)] FATAL: manifest promote failed" >&2
    rm -f "$manifest_tmp"
    return 1
  fi
  echo "[$(date -Iseconds)] Backup manifest written to $manifest" >&2
  return 0
}

# publish_manifest_signature — sign the manifest this run wrote, then arm the ratchet.
# Deployments with no backup key have nothing to sign with and are skipped here; STEP 2
# of restore.sh skips the verification symmetrically. The marker is written only after
# a signature exists, so the requirement can never arm without one.
publish_manifest_signature() {
  local manifest="${BACKUP_DIR}/manifest_${TIMESTAMP}.json"
  [ "$ENCRYPT" -eq 1 ] || return 0
  if [ ! -f "$manifest" ]; then
    echo "[$(date -Iseconds)] FATAL: cannot sign missing backup manifest" >&2
    return 1
  fi
  if ! sign_manifest "$manifest"; then
    echo "[$(date -Iseconds)] FATAL: could not sign the backup manifest" >&2
    return 1
  fi
  echo "[$(date -Iseconds)] Backup manifest signature written to ${manifest}.hmac" >&2
  if [ ! -d "$HOST_SECRETS_DIR" ]; then
    echo "[$(date -Iseconds)] FATAL: cannot publish the signed-manifest requirement in ${HOST_SECRETS_DIR}" >&2
    return 1
  fi
  if [ ! -e "$MANIFEST_HMAC_MARKER" ] && ! : > "$MANIFEST_HMAC_MARKER" 2>/dev/null; then
    echo "[$(date -Iseconds)] FATAL: could not require signed manifests in ${HOST_SECRETS_DIR}" >&2
    return 1
  fi
  return 0
}

finalize_backup() {
  MANIFEST_STATE="failed"
  if ! write_manifest; then
    if ! discard_current_backup; then
      echo "[$(date -Iseconds)] FATAL: could not discard incomplete backup ${TIMESTAMP}" >&2
    fi
    return 1
  fi
  MANIFEST_STATE="ok"
  if [ "$ENCRYPT" -eq 1 ]; then
    MANIFEST_SIGNATURE_STATE="failed"
    if ! publish_manifest_signature; then
      if ! discard_current_backup; then
        echo "[$(date -Iseconds)] FATAL: could not discard unsigned backup ${TIMESTAMP}" >&2
      fi
      return 1
    fi
    MANIFEST_SIGNATURE_STATE="ok"
  fi
  return 0
}

if ! finalize_backup; then
  echo "[$(date -Iseconds)] FATAL: backup finalization failed for timestamp ${TIMESTAMP}" >&2
  exit 1
fi

# --- Optional S3 upload ------------------------------------------------------
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "[$(date -Iseconds)] aws CLI not available; skipping S3 upload (install awscli or use the amazon/aws-cli sidecar)"
  else
    for f in "$JARVIS_BACKUP_FILE" "$LITELLM_BACKUP_FILE" "$PDFS_BACKUP_FILE" "$SECRETS_BACKUP_FILE" \
             "$BACKUP_DIR"/manifest_"${TIMESTAMP}".json \
             "$BACKUP_DIR"/manifest_"${TIMESTAMP}".json.hmac \
             "$BACKUP_DIR"/qdrant_*_"${TIMESTAMP}".snapshot \
             "$BACKUP_DIR"/qdrant_*_"${TIMESTAMP}".snapshot.enc; do
      [ -n "$f" ] && [ -f "$f" ] || continue
      aws s3 cp "$f" "s3://${BACKUP_S3_BUCKET}/$(basename "$f")"
    done
    echo "[$(date -Iseconds)] Uploaded backups to s3://${BACKUP_S3_BUCKET}/"
  fi
fi

# --- UI retention config (system-wide /backup-trigger/.retention.json, written
# by the admin API) refines the env defaults: `max_age_days` overrides the age
# window and `keep_last_n` caps the number of restore points kept. Both may be
# absent/null -> env fallback only. The helpers below share their shape with
# scripts/prune.sh (in-flight refusal) — kept inline to avoid a second mount.
# TRIGGER_DIR is defined once near the maintenance skip-guard above.
RETENTION_FILE="${TRIGGER_DIR}/.retention.json"
RESTORE_REQUEST_FILE="${TRIGGER_DIR}/.restore_request.json"
RESTORE_STATUS_FILE="${TRIGGER_DIR}/.restore_status.json"
UPDATE_PIN_FILE="${LOCK_DIR}/update-backup-pin.json"

# resolve_retention_days <retention_file> <env_default> — the effective age
# window in days. A UI max_age_days >= 1 overrides the env default; 0, absent, or
# non-numeric is a no-op (the env default stands) so a stray 0 can never become
# `find -mtime +0 -delete`, which would reap every archive older than ~24h. This
# mirrors the keep_last_n < 1 no-op floor in retention_keep_last_n.
resolve_retention_days() {
  local file="$1" env_default="$2" ui
  ui="$(grep -oE '"max_age_days"[[:space:]]*:[[:space:]]*[0-9]+' "$file" 2>/dev/null | grep -oE '[0-9]+$' | head -1 || true)"
  if [ -n "${ui:-}" ] && [ "$ui" -ge 1 ] 2>/dev/null; then
    printf '%s' "$ui"
  else
    printf '%s' "$env_default"
  fi
}

KEEP_LAST_N=""
if [ -f "$RETENTION_FILE" ]; then
  RETENTION_DAYS="$(resolve_retention_days "$RETENTION_FILE" "$RETENTION_DAYS")"
  ui_keep_n="$(grep -oE '"keep_last_n"[[:space:]]*:[[:space:]]*[0-9]+' "$RETENTION_FILE" 2>/dev/null | grep -oE '[0-9]+$' | head -1 || true)"
  [ -n "${ui_keep_n:-}" ] && KEEP_LAST_N="$ui_keep_n"
fi

# prune_in_flight_ts — every timestamp a present restore or update is using, one
# per line, so neither age nor keep-last-N pruning can remove a rollback point.
# (Belt-and-braces: the safety pre-backup already sets BACKUP_SKIP_PRUNE and the
# single-threaded sidecar loop never backs up during a restore.) Mirrors the same
# helper in scripts/prune.sh.
prune_in_flight_ts() {
  local f pin
  for f in "${RESTORE_REQUEST_FILE:-}" "${RESTORE_STATUS_FILE:-}"; do
    [ -n "$f" ] || continue
    [ -f "$f" ] || continue
    grep -oE '"(timestamp|safety_backup_ts)"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' "$f" 2>/dev/null \
      | grep -oE '[0-9]{8}_[0-9]{6}' || true
  done
  if [ -n "${UPDATE_PIN_FILE:-}" ] && [ -f "$UPDATE_PIN_FILE" ] \
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

# retention_prune_age <backup_dir> <days> <in_flight_ts_newline_list>
# Apply the age window one exact file at a time so in-flight restore/update
# timestamps survive as complete sets. Dot-directories used for staged restore
# safety copies do not match these archive names and are never considered.
retention_prune_age() {
  local dir="$1" days="$2" in_flight="$3" path base ts
  [ "$days" -ge 0 ] 2>/dev/null || return 1
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    base="$(basename "$path")"
    ts="$(printf '%s' "$base" | grep -oE '[0-9]{8}_[0-9]{6}' | tail -1 || true)"
    if [ -n "$ts" ] && grep -qxF "$ts" <<<"$in_flight"; then
      echo "[$(date -Iseconds)] age-${days}: not pruning ${ts} (in-flight restore/update)"
      continue
    fi
    rm -f -- "$path"
  done < <(find "$dir" -maxdepth 1 \( -type f -o -type l \) \( \
      -name 'jarvis_*.sql.gz' -o -name 'jarvis_*.sql.gz.enc' \
      -o -name 'litellm_*.sql.gz' -o -name 'litellm_*.sql.gz.enc' \
      -o -name 'pdfs_*.tar.gz' -o -name 'pdfs_*.tar.gz.enc' \
      -o -name 'secrets_*.tar.gz' -o -name 'secrets_*.tar.gz.enc' \
      -o -name 'qdrant_*.snapshot' -o -name 'qdrant_*.snapshot.enc' \
      -o -name 'manifest_*.json' -o -name 'manifest_*.json.hmac' \
      -o -name '*.tmp' -o -name '*.raw' \
    \) -mtime "+${days}" -print)
}

# retention_keep_last_n <backup_dir> <keep_n> <in_flight_ts_newline_list>
# Delete every archive file whose restore-point timestamp falls past the newest
# <keep_n> jarvis_* dumps (one dump == one restore point). The newest N are kept,
# so the just-taken backup is never pruned. keep_n < 1 (or non-numeric) is a no-op.
retention_keep_last_n() {
  local dir="$1" keep="$2" in_flight="$3" ts n=0
  [ "$keep" -ge 1 ] 2>/dev/null || return 0
  while IFS= read -r ts; do
    [ -n "$ts" ] || continue
    n=$((n + 1))
    [ "$n" -le "$keep" ] && continue
    if grep -qxF "$ts" <<<"$in_flight"; then
      echo "[$(date -Iseconds)] keep-last-${keep}: not pruning ${ts} (in-flight restore/update)"
      continue
    fi
    find "$dir" -maxdepth 1 \( \
        -name "jarvis_${ts}.sql.gz" -o -name "jarvis_${ts}.sql.gz.enc" \
        -o -name "litellm_${ts}.sql.gz" -o -name "litellm_${ts}.sql.gz.enc" \
        -o -name "pdfs_${ts}.tar.gz" -o -name "pdfs_${ts}.tar.gz.enc" \
        -o -name "secrets_${ts}.tar.gz" -o -name "secrets_${ts}.tar.gz.enc" \
        -o -name "qdrant_*_${ts}.snapshot" -o -name "qdrant_*_${ts}.snapshot.enc" \
        -o -name "manifest_${ts}.json" -o -name "manifest_${ts}.json.hmac" \
      \) -delete
  done < <(find "$dir" -maxdepth 1 -type f \
        \( -name 'jarvis_*.sql.gz' -o -name 'jarvis_*.sql.gz.enc' \) -printf '%f\n' 2>/dev/null \
      | grep -oE '^jarvis_[0-9]{8}_[0-9]{6}\.sql\.gz(\.enc)?$' \
      | grep -oE '[0-9]{8}_[0-9]{6}' | sort -ru)
}

# --- Prune old backups (DB dumps, secrets archives, Qdrant snapshots) --------
# Gated on BACKUP_SKIP_PRUNE: restore.sh's safety pre-backup sets it so the prune
# can never delete the archive being restored (an old `local` target older than
# the age window would otherwise be eligible). max_age_days (if >= 1) has already
# overridden RETENTION_DAYS above; keep_last_n additionally caps the restore-point
# count after the age prune.
if [ -z "${BACKUP_SKIP_PRUNE:-}" ]; then
  # The updater owns update.lock while selecting and publishing a rollback
  # pin. This producer already owns backup.lock, so it must never wait for the
  # lifecycle lock: skipping one prune avoids lock-order deadlock and data loss.
  retention_lock_dir="${BACKUP_DIR}/.lifecycle"
  retention_update_lock="${retention_lock_dir}/update.lock"
  if [ -L "$retention_lock_dir" ] \
     || ! mkdir -p "$retention_lock_dir" \
     || [ ! -d "$retention_lock_dir" ] \
     || [ -L "$retention_update_lock" ]; then
    echo "[backup] Retention prune skipped (unsafe update mutex)"
    exit 0
  fi
  chmod 700 "$retention_lock_dir"
  exec 8>>"$retention_update_lock"
  if ! flock -n 8; then
    echo "[$(date -Iseconds)] Retention prune skipped (update is publishing a rollback pin)"
  else
    IN_FLIGHT_TIMESTAMPS="$(prune_in_flight_ts)"
    if [ -n "${TIMESTAMP:-}" ] \
       && ! grep -qxF "$TIMESTAMP" <<<"$IN_FLIGHT_TIMESTAMPS"; then
      IN_FLIGHT_TIMESTAMPS="${IN_FLIGHT_TIMESTAMPS}${IN_FLIGHT_TIMESTAMPS:+$'\n'}${TIMESTAMP}"
    fi
    retention_prune_age "$BACKUP_DIR" "$RETENTION_DAYS" "$IN_FLIGHT_TIMESTAMPS"
    echo "[$(date -Iseconds)] Pruned backups older than ${RETENTION_DAYS} days"
    if [ -n "${KEEP_LAST_N:-}" ]; then
      retention_keep_last_n "$BACKUP_DIR" "$KEEP_LAST_N" "$IN_FLIGHT_TIMESTAMPS"
      echo "[$(date -Iseconds)] keep-last-${KEEP_LAST_N} retention applied"
    fi
  fi
else
  echo "[$(date -Iseconds)] Retention prune skipped (BACKUP_SKIP_PRUNE set)"
fi
