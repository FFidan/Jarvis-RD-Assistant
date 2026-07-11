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
#       (the Docker-secret source files; without these an encrypted DB backup
#        is undecryptable. Always encrypted when a backup key is configured.)
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
    --exclude "*" --include "*_${ts}.*" --include "manifest_${ts}.json"
  echo "[$(date -Iseconds)] Pulled backup ${ts} from s3://${BACKUP_S3_BUCKET}/ into ${dest}" >&2
}

if [ -n "${BACKUP_PULL_TS:-}" ]; then
  pull_from_s3 "$BACKUP_PULL_TS" "${BACKUP_PULL_DEST:-/restore-inbox}"
  exit 0
fi

# Read PGPASSWORD from Docker Secret — required; fail fast if missing.
if [ ! -r /run/secrets/postgres_password ]; then
    echo "FATAL: cannot read /run/secrets/postgres_password" >&2
    exit 1
fi
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
# When set (e.g. by restore.sh's safety pre-backup) the retention prune at the
# tail is skipped, so a pre-restore safety backup can never delete the very
# archive being restored (a `local` archive older than RETENTION_DAYS).
BACKUP_SKIP_PRUNE="${BACKUP_SKIP_PRUNE:-}"
# When set (by restore.sh's safety pre-backup) the maintenance skip-guard and the
# flock mutex below are BYPASSED: the restore itself holds .maintenance, so its own
# safety snapshot must run rather than treat that sentinel as "another restore is
# mid-flight" and abort the very backup the restore depends on.
BACKUP_FORCE="${BACKUP_FORCE:-}"
ENC_KEYFILE="${BACKUP_ENCRYPT_KEYFILE:-}"
LITELLM_DATABASE="${LITELLM_DATABASE:-litellm}"
QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"
QDRANT_API_KEYFILE="${QDRANT_API_KEYFILE:-/run/secrets/qdrant_api_key}"
SECRETS_DIR="${SECRETS_DIR:-/secrets}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# When a backup encryption key is configured archives get a .enc suffix and are
# piped through openssl; otherwise they are written in the clear.
if [ -n "$ENC_KEYFILE" ] && [ -s "$ENC_KEYFILE" ]; then
  ENCRYPT=1
else
  ENCRYPT=0
fi

mkdir -p "$BACKUP_DIR"

# On-demand trigger: the WebUI Backup panel writes /backup-trigger/.backup_now to
# request an immediate run. The sidecar loop's `sleep` means a run may already be
# in progress; clearing the flag at the top of this run consumes the request so
# the loop does not re-fire on its next iteration. (A scheduled run also clears a
# stale flag — harmless.)
TRIGGER_FILE="${BACKUP_TRIGGER_DIR:-/backup-trigger}/.backup_now"
rm -f "$TRIGGER_FILE" 2>/dev/null || true

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
SECRETS_STATE="skipped"
QDRANT_STATE="skipped"

# write_last_run — emit ${BACKUP_DIR}/.last_run.json (a dotfile, NOT an archive,
# so the router's allowlist/globs never match it).
write_last_run() {
  local succeeded="false"
  [ "$JARVIS_STATE" = "ok" ] && [ "$LITELLM_STATE" = "ok" ] && succeeded="true"
  local enc="false"
  [ "$ENCRYPT" -eq 1 ] && enc="true"
  # skipped_maintenance distinguishes a run that intentionally stood down for an
  # in-flight restore (succeeded stays false, but it is NOT a failure) from a real
  # backup failure, so /status can word the two differently.
  local skipped="false"
  [ "${SKIPPED_MAINTENANCE:-0}" = "1" ] && skipped="true"
  local lr_tmp="${BACKUP_DIR}/.last_run.json.tmp"
  cat > "$lr_tmp" <<JSON
{"attempted_at":"${ATTEMPTED_AT}","timestamp":"${TIMESTAMP}","succeeded":${succeeded},"encrypted":${enc},"skipped_maintenance":${skipped},"retention_days":${RETENTION_DAYS},"stores":{"jarvis":"${JARVIS_STATE}","litellm":"${LITELLM_STATE}","secrets":"${SECRETS_STATE}","qdrant":"${QDRANT_STATE}"}}
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
TRIGGER_DIR="${BACKUP_TRIGGER_DIR:-/backup-trigger}"
MAINTENANCE_SENTINEL="${MAINTENANCE_SENTINEL:-${TRIGGER_DIR}/.maintenance}"
MAINTENANCE_DESTRUCTIVE="${MAINTENANCE_DESTRUCTIVE_SENTINEL:-${TRIGGER_DIR}/.destructive}"
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

if [ -z "$BACKUP_FORCE" ] && maintenance_active; then
  SKIPPED_MAINTENANCE=1
  echo "[backup] skipped: a restore holds the maintenance sentinel" >&2
  exit 0
fi

# flock single-run mutex (belt-and-braces against a manual invocation racing a
# restore or a second scheduled run). The forced safety backup never takes this
# lock: restore.sh does not hold it and the safety backup must never block on it.
if [ -z "$BACKUP_FORCE" ] && [ -d "$TRIGGER_DIR" ]; then
  exec 9>"${TRIGGER_DIR}/.backup.lock"
  if ! flock -n 9; then
    echo "[backup] another backup is already running; skipping" >&2
    exit 0
  fi
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
  mv "$tmp" "$out"
  # Log to stderr so command substitution captures only the filename below.
  echo "[$(date -Iseconds)] Backup saved to $out ($(du -h "$out" | cut -f1))" >&2
  printf '%s' "$out"
}

echo "[$(date -Iseconds)] Starting backup..."

# --- PostgreSQL: jarvis (primary) + litellm (API keys / virtual keys / spend) -
JARVIS_BACKUP_FILE="$(dump_db "${PGDATABASE:-jarvis}" jarvis)"
JARVIS_STATE="ok"
LITELLM_BACKUP_FILE="$(dump_db "$LITELLM_DATABASE" litellm)"
LITELLM_STATE="ok"

# --- secrets/ directory ------------------------------------------------------
# The Docker-secret source files; an encrypted DB backup is useless without the
# key that decrypts it. Always encrypted when a backup key is configured.
# When no key is set: refuse outright in production (plaintext secrets on disk
# is unacceptable); silently skip in non-production with a clear warning.
ENVIRONMENT="${ENVIRONMENT:-development}"
SECRETS_BACKUP_FILE=""
if [ -d "$SECRETS_DIR" ]; then
  if [ "$ENCRYPT" -eq 1 ]; then
    SECRETS_BACKUP_FILE="${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz.enc"
    secrets_tmp="${SECRETS_BACKUP_FILE}.tmp"
    # Exclude the backup encryption key from its own encrypted archive: sealing
    # the key inside the .enc it unlocks is circularly undecryptable after total
    # host loss. The operator must hold this key out-of-band (see DEPLOYMENT.md).
    # The on-disk key is backup_encrypt_key.txt (the ./secrets source for the
    # backup_encrypt_key Docker Secret); tar --exclude globs member names, so the
    # pattern must carry the .txt — a keyless ./backup_encrypt_key is a silent no-op.
    tar -czf - -C "$SECRETS_DIR" --exclude=./backup_encrypt_key.txt . \
      | encrypt_or_passthrough > "$secrets_tmp"
    secrets_st=("${PIPESTATUS[@]}")
    if [ "${secrets_st[0]}" -ne 0 ] || [ "${secrets_st[1]}" -ne 0 ]; then
      rm -f "$secrets_tmp"
      SECRETS_STATE="failed"
      echo "FATAL: secrets archive failed (tar=${secrets_st[0]} enc=${secrets_st[1]})" >&2
      exit 1
    fi
    chmod 600 "$secrets_tmp"
    mv "$secrets_tmp" "$SECRETS_BACKUP_FILE"
    SECRETS_STATE="ok"
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
            chmod 600 "$qsnap_tmp"; mv "$qsnap_tmp" "$qsnap_out"; rm -f "$qsnap_raw"
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
# write_manifest — emit ${BACKUP_DIR}/manifest_${TIMESTAMP}.json listing every
# archive this run produced (filename, sha256, size) plus app_version and the
# applied schema_version, so the restore UI can show per-restore-point version
# compatibility. PLAINTEXT METADATA ONLY (filenames, hex digests, integers — no
# secrets); it is not matched by the router's archive globs, so it is never
# listed or downloaded as a backup.
#
# Failure-tolerant: a failed psql/sha256sum/write WARNs and returns 0 so it can
# never regress an already-successful backup. Called as `write_manifest || true`
# below, which also disables errexit for the whole function body.
write_manifest() {
  local schema_version app_version created_at manifest manifest_tmp
  local first f base sum size
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
  {
    printf '{"timestamp":"%s","app_version":"%s","schema_version":%s,"created_at":"%s","archives":[' \
      "$TIMESTAMP" "$app_version" "$schema_version" "$created_at"
    first=1
    for f in "$BACKUP_DIR"/*"${TIMESTAMP}"*; do
      [ -f "$f" ] || continue
      base="$(basename "$f")"
      case "$base" in
        manifest_*|*.tmp|*.raw) continue ;;
      esac
      sum="$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)" || sum=""
      size="$(stat -c%s "$f" 2>/dev/null)" || size=0
      [ "$first" -eq 1 ] || printf ','
      first=0
      printf '{"filename":"%s","sha256":"%s","size_bytes":%s}' "$base" "$sum" "$size"
    done
    printf ']}'
  } > "$manifest_tmp" 2>/dev/null || {
    echo "[$(date -Iseconds)] WARNING: manifest write failed; continuing" >&2
    rm -f "$manifest_tmp"
    return 0
  }
  chmod 644 "$manifest_tmp" 2>/dev/null || true
  mv -f "$manifest_tmp" "$manifest" 2>/dev/null || {
    echo "[$(date -Iseconds)] WARNING: manifest promote failed; continuing" >&2
    rm -f "$manifest_tmp"
    return 0
  }
  echo "[$(date -Iseconds)] Backup manifest written to $manifest" >&2
}
write_manifest || true

# --- Optional S3 upload ------------------------------------------------------
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "[$(date -Iseconds)] aws CLI not available; skipping S3 upload (install awscli or use the amazon/aws-cli sidecar)"
  else
    for f in "$JARVIS_BACKUP_FILE" "$LITELLM_BACKUP_FILE" "$SECRETS_BACKUP_FILE" \
             "$BACKUP_DIR"/manifest_"${TIMESTAMP}".json \
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

# prune_in_flight_ts — every timestamp a present restore is using (its target +
# safety backup), one per line, so keep-last-N never prunes a restore's source.
# (Belt-and-braces: the safety pre-backup already sets BACKUP_SKIP_PRUNE and the
# single-threaded sidecar loop never backs up during a restore.) Mirrors the same
# helper in scripts/prune.sh.
prune_in_flight_ts() {
  local f
  for f in "$RESTORE_REQUEST_FILE" "$RESTORE_STATUS_FILE"; do
    [ -f "$f" ] || continue
    grep -oE '"(timestamp|safety_backup_ts)"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' "$f" 2>/dev/null \
      | grep -oE '[0-9]{8}_[0-9]{6}' || true
  done
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
      echo "[$(date -Iseconds)] keep-last-${keep}: not pruning ${ts} (in-flight restore)"
      continue
    fi
    find "$dir" -maxdepth 1 \( \
        -name "jarvis_${ts}.sql.gz" -o -name "jarvis_${ts}.sql.gz.enc" \
        -o -name "litellm_${ts}.sql.gz" -o -name "litellm_${ts}.sql.gz.enc" \
        -o -name "secrets_${ts}.tar.gz" -o -name "secrets_${ts}.tar.gz.enc" \
        -o -name "qdrant_*_${ts}.snapshot" -o -name "qdrant_*_${ts}.snapshot.enc" \
        -o -name "manifest_${ts}.json" \
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
  find "$BACKUP_DIR" \( \
      -name "jarvis_*.sql.gz" -o -name "jarvis_*.sql.gz.enc" \
      -o -name "litellm_*.sql.gz" -o -name "litellm_*.sql.gz.enc" \
      -o -name "secrets_*.tar.gz" -o -name "secrets_*.tar.gz.enc" \
      -o -name "qdrant_*.snapshot" -o -name "qdrant_*.snapshot.enc" \
      -o -name "manifest_*.json" \
      -o -name "*.tmp" -o -name "*.raw" \
    \) -mtime "+${RETENTION_DAYS}" -delete
  echo "[$(date -Iseconds)] Pruned backups older than ${RETENTION_DAYS} days"
  if [ -n "${KEEP_LAST_N:-}" ]; then
    retention_keep_last_n "$BACKUP_DIR" "$KEEP_LAST_N" "$(prune_in_flight_ts)"
    echo "[$(date -Iseconds)] keep-last-${KEEP_LAST_N} retention applied"
  fi
else
  echo "[$(date -Iseconds)] Retention prune skipped (BACKUP_SKIP_PRUNE set)"
fi
