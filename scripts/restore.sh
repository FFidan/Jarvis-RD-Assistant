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
# Durable crash-recovery marker for the rename-swap: names the db + phase of the
# in-flight swap so a sidecar restart (via `restore.sh --recover`) can reconcile a
# stranded half-swap deterministically. Cleared when a db's swap fully completes.
SWAP_STATE_FILE="${TRIGGER_DIR}/.restore_swap_state.json"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
# Off-host (cross-host) disaster recovery: the operator drops the archive set +
# a one-time operator key into the rw restore_inbox volume mounted here. This is
# the ONLY writable cross-host secrets-staging surface and is NEVER under the RO
# /secrets mount. Used only by an `inbox` restore (see STEP 1 / STEP 8); the
# same-host WebUI restore never touches any of these paths.
INBOX_DIR="${RESTORE_INBOX_DIR:-/restore-inbox}"
OPERATOR_KEYFILE="${INBOX_DIR}/operator_key"
SECRETS_STAGING="${INBOX_DIR}/.secrets-staging"
# Off-host restore materializes the restored ./secrets/*.txt here (the sidecar's rw
# bind mount of the host ./secrets) so the app containers self-restart onto the
# rotated secrets — a fresh host recovers with zero terminal steps. Only the inbox
# path mounts this; a local restore never does.
HOST_SECRETS_DIR="${HOST_SECRETS_DIR:-/host-secrets}"

PGHOST="${PGHOST:-postgres}"
PGUSER="${PGUSER:-jarvis}"
JARVIS_DB="${PGDATABASE:-jarvis}"
LITELLM_DB="${LITELLM_DATABASE:-litellm}"
# The postgres data volume is mounted here read-only (compose: postgres_data:ro) so
# the disk preflight can size the free space the reload will consume. Overridable
# only to keep the path in one place; users never set it.
POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-/postgres-data}"
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
HEARTBEAT_PID=""
MANUAL_STEPS_REQUIRED=0
# Set only by the --recover entrypoint branch: skips the request re-consume in the
# EXIT trap so a crash-recovery run never eats a legitimately pending restore.
RECOVER_MODE=0
# Set only by the --inbox-manifest entrypoint branch: a read-only inventory pass that
# short-circuits the EXIT trap entirely (no request consume, no status write, no key
# shred, no maintenance change) — it runs every sidecar loop and must touch nothing.
MANIFEST_MODE=0
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

# --- resolve_secrets_archive — echo the secrets_<ts> archive path in ${ARCHIVE_DIR}
#     (prefer the .enc form), or nothing (return 1) if absent. Single source shared by
#     the STEP-2 secrets preflight (fail BEFORE any destruction when it is missing) and
#     STEP 8 (materialize it), so the two can never diverge on what "has secrets" means.
resolve_secrets_archive() {
  local cand
  for cand in "${ARCHIVE_DIR}/secrets_${TIMESTAMP}.tar.gz.enc" \
              "${ARCHIVE_DIR}/secrets_${TIMESTAMP}.tar.gz"; do
    if [ -f "$cand" ]; then printf '%s' "$cand"; return 0; fi
  done
  return 1
}

# --- safety_backup_is_fresh <backup_rc> — true iff the STEP-4 safety pre-backup is a
#     usable rollback point FOR THIS RUN: backup.sh exited 0, ${BACKUP_DIR}/.last_run.json
#     records succeeded:true, and its attempted_at is NEWER than this restore's start
#     (STARTED_AT). Freshness matters: a safety backup that fails on a full/read-only
#     /backups cannot rewrite .last_run.json, so YESTERDAY's succeeded:true record
#     survives — a stale succeeded record must NOT count as a fresh rollback point.
safety_backup_is_fresh() {
  local rc="$1" lr succeeded attempted
  [ "$rc" -eq 0 ] || return 1
  lr="$(cat "${BACKUP_DIR}/.last_run.json" 2>/dev/null || true)"
  succeeded="$(printf '%s' "$lr" \
    | grep -oE '"succeeded"[[:space:]]*:[[:space:]]*(true|false)' | grep -oE 'true|false' | head -1 || true)"
  [ "$succeeded" = "true" ] || return 1
  attempted="$(printf '%s' "$lr" \
    | grep -oE '"attempted_at"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/' | head -1 || true)"
  [ -n "$attempted" ] || return 1
  [[ "$attempted" > "$STARTED_AT" ]]
}

# --- write_inbox_manifest — inventory ${INBOX_DIR} into a SANITIZED
#     ${TRIGGER_DIR}/.inbox_manifest.json (names/booleans ONLY — never a path or a key
#     byte) that the admin app's GET /inbox lists. Groups valid_archive_name-accepted
#     files by their %Y%m%d_%H%M%S timestamp; per group emits
#     {timestamp, complete, has_secrets, has_key} where complete = the jarvis + litellm
#     DB archives are BOTH present (mirrors restore.sh's own STEP-1 completeness gate),
#     has_secrets = a secrets_<ts> archive is present, has_key = the one-time operator
#     key is present (a single inbox-wide fact). Atomic tmp->mv; writes [] on an empty
#     inbox. Never touches the DB, never consumes the restore request, never emits a
#     path or key content. Called ONLY from the --inbox-manifest branch (MANIFEST_MODE).
write_inbox_manifest() {
  local out="${TRIGGER_DIR}/.inbox_manifest.json"
  local tmp="${out}.tmp"
  local has_key="false"
  [ -s "$OPERATOR_KEYFILE" ] && has_key="true"

  # Distinct, sorted timestamps of every allow-listed archive in the inbox. The ts is
  # extracted immediately before the known extension (mirrors backups.py:_TS_RE) so a
  # collection name that itself contains digits cannot be misread as the timestamp.
  local timestamps="" f base ts
  shopt -s nullglob
  for f in "${INBOX_DIR}"/*; do
    base="$(basename "$f")"
    valid_archive_name "$base" || continue
    ts="$(printf '%s' "$base" \
      | sed -nE 's/.*_([0-9]{8}_[0-9]{6})\.(sql\.gz|tar\.gz|snapshot)(\.enc)?$/\1/p')"
    [ -n "$ts" ] && timestamps="${timestamps}${ts}"$'\n'
  done
  shopt -u nullglob
  timestamps="$(printf '%s' "$timestamps" | sort -u)"

  {
    printf '['
    local first=1 t complete has_secrets
    while IFS= read -r t; do
      [ -n "$t" ] || continue
      complete="false"
      has_secrets="false"
      # complete requires the manifest too: restore.sh STEP 2 HARD-requires
      # manifest_<ts>.json for an inbox restore, so a DB-complete point that is missing
      # its manifest is NOT restorable (it fails pre-swap) and must not read complete.
      if { [ -f "${INBOX_DIR}/jarvis_${t}.sql.gz" ] || [ -f "${INBOX_DIR}/jarvis_${t}.sql.gz.enc" ]; } \
         && { [ -f "${INBOX_DIR}/litellm_${t}.sql.gz" ] || [ -f "${INBOX_DIR}/litellm_${t}.sql.gz.enc" ]; } \
         && [ -f "${INBOX_DIR}/manifest_${t}.json" ]; then
        complete="true"
      fi
      if [ -f "${INBOX_DIR}/secrets_${t}.tar.gz" ] || [ -f "${INBOX_DIR}/secrets_${t}.tar.gz.enc" ]; then
        has_secrets="true"
      fi
      [ "$first" = "1" ] || printf ','
      first=0
      printf '{"timestamp":"%s","complete":%s,"has_secrets":%s,"has_key":%s}' \
        "$t" "$complete" "$has_secrets" "$has_key"
    done <<< "$timestamps"
    printf ']'
  } > "$tmp" 2>/dev/null || return 0
  mv -f "$tmp" "$out" 2>/dev/null || return 0
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

# --- rename-swap state file (durable, drives --recover) ----------------------
write_swap_state() {
  # $1 = db, $2 = phase (reload_tmp|swapping_out|swapping_in|verified). Written
  # BEFORE each transition so a crash leaves a durable record of the db + step.
  printf '{"db":"%s","phase":"%s"}' "$(_json_escape "$1")" "$(_json_escape "$2")" \
    > "${SWAP_STATE_FILE}.tmp" 2>/dev/null || return 0
  mv -f "${SWAP_STATE_FILE}.tmp" "$SWAP_STATE_FILE" 2>/dev/null || return 0
}

clear_swap_state() {
  rm -f "$SWAP_STATE_FILE" 2>/dev/null || true
}

read_swap_db() {
  # Emit the db name recorded in the swap-state file, or nothing if absent.
  # The swap-state file lives on the rw backup_trigger volume, which an app
  # container can write, so its `db` value is attacker-controllable. It flows into
  # single-quoted psql literals downstream (db_exists / revert_swap), so a raw value
  # like x'; DROP DATABASE ... could inject SQL: allowlist it to the two known DB
  # names here, emitting nothing otherwise (the caller's -z guard then no-ops).
  local db
  [ -r "$SWAP_STATE_FILE" ] || return 0
  db="$(grep -oE '"db"[[:space:]]*:[[:space:]]*"[^"]*"' "$SWAP_STATE_FILE" 2>/dev/null \
    | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/' | head -1 || true)"
  case "$db" in
    "$JARVIS_DB"|"$LITELLM_DB") printf '%s' "$db" ;;
  esac
}

# db_exists <name> — true iff a database with that exact name is in pg_database.
# Uses its own non-ON_ERROR_STOP connection so it never aborts the script.
db_exists() {
  local out
  out="$(psql -h "$PGHOST" -U "$PGUSER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${1}'" 2>/dev/null || true)"
  [ "$out" = "1" ]
}

# verify_db_structural <db> <is_jarvis> — the SOLE post-swap gate. Proves the
# reload populated a real schema (not an empty/partial db) using db-appropriate
# tables: jarvis has OUR schema_migrations + auth tables; litellm is a THIRD-PARTY
# (Prisma) schema with LiteLLM_* tables and NO schema_migrations. Returns non-zero
# on any failure, which drives the revert. NEVER polls the app /health: /health
# aggregates every dependency (incl. a mid-restore litellm) and 503s under
# maintenance, so polling it would revert successful restores.
verify_db_structural() {
  local db="$1" is_jarvis="$2" regs
  if [ "$is_jarvis" = "1" ]; then
    psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -tAc \
      "SELECT 1 FROM schema_migrations LIMIT 1;" >/dev/null 2>&1 || return 1
    regs="$(psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -tAc \
      "SELECT (to_regclass('public.users') IS NOT NULL AND to_regclass('public.sessions') IS NOT NULL);" \
      2>/dev/null || true)"
    [ "$regs" = "t" ] || return 1
  else
    # litellm keyed on schema_migrations would fail EVERY restore (that table does
    # not exist there — verified against the live db). Assert its table set exists
    # instead; version-robust (any LiteLLM_* table, not a name a litellm upgrade
    # might rename).
    regs="$(psql -h "$PGHOST" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -tAc \
      "SELECT (count(*) > 0) FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'LiteLLM%';" \
      2>/dev/null || true)"
    [ "$regs" = "t" ] || return 1
  fi
  return 0
}

# preflight_disk_or_fail — refuse a restore that could exhaust the database volume
# BEFORE creating any tmp db. The swap keeps BOTH live dbs in place and reloads a
# transient <db>_restore_tmp per db, then swaps by rename (catalog-only, no new
# space); the <db>_pre_restore snapshot reuses the live db's existing space. So the
# NEW space a restore consumes is just the two tmp dbs + headroom, and the check is
# free space > both tmp dbs + headroom — the live dbs are already resident, not
# free, so counting them again would demand ~2x the live size and false-FAIL a
# legitimate restore on a >50%-full volume. The tmp estimate is ADDITIVE per db
# (FRESH_DB_FLOOR + gz x CONTENT_FACTOR): a single multiplier is unsafe because a
# tiny db is dominated by the fixed base-db overhead (measured on pg16.8: jarvis
# 18.8x, litellm 266x). It is deliberately conservative — it covers reload WAL
# amplification + FSM/VM/temp overhead, and a cluster-wide ENOSPC mid-reload would
# stall WAL for the LIVE db too, so fail-fast is safer than try-and-see.
preflight_disk_or_fail() {
  local fresh_floor_kb=$((100 * 1024)) content_factor=30 headroom_kb=$((2 * 1024 * 1024))
  local tmp_est_kb req_kb avail_kb jarvis_gz litellm_gz
  jarvis_gz="$(stat -c%s "$JARVIS_ARCHIVE" 2>/dev/null || echo 0)"
  litellm_gz="$(stat -c%s "$LITELLM_ARCHIVE" 2>/dev/null || echo 0)"
  tmp_est_kb=$(( fresh_floor_kb + jarvis_gz * content_factor / 1024 \
              + fresh_floor_kb + litellm_gz * content_factor / 1024 ))
  req_kb=$(( tmp_est_kb + headroom_kb ))
  avail_kb="$(df -Pk "$POSTGRES_DATA_DIR" 2>/dev/null | awk 'NR==2{print $4}')"
  # A non-numeric result means df could not read the volume — almost always the
  # read-only postgres_data mount is missing. Fail with a diagnosable message
  # rather than a misleading "0 GB free" (fail-closed is correct for a destructive
  # op; an ENOSPC mid-reload is non-destructive but stalls the live DB's WAL).
  case "$avail_kb" in
    ''|*[!0-9]*)
      fail_before_destruction "cannot read free space on the database volume ${POSTGRES_DATA_DIR} (is the postgres_data read-only mount present?); refusing the restore" ;;
  esac
  if [ "$avail_kb" -le "$req_kb" ]; then
    fail_before_destruction "insufficient disk for a safe restore: need ~$(( req_kb / 1024 / 1024 )) GB free on the database volume, have ~$(( avail_kb / 1024 / 1024 )) GB"
  fi
}

# revert_swap <db> — roll production back to the untouched <db>_pre_restore after a
# post-swap verify failure. The renamed-out snapshot inherited ALLOW_CONNECTIONS
# false from the disallow step (proven on pg16.8), so it MUST be re-enabled BEFORE
# the rename-back or the restored-to-original db stays non-servable. Best-effort
# throughout (the caller already holds maintenance via DROP_STARTED=1).
revert_swap() {
  local db="$1"
  local tmp="${db}_restore_tmp" pre="${db}_pre_restore"
  if db_exists "$db"; then
    psql_admin "ALTER DATABASE \"${db}\" WITH ALLOW_CONNECTIONS false;" >/dev/null 2>&1 || true
    psql_admin "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${db}' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true
    psql_admin "ALTER DATABASE \"${db}\" RENAME TO \"${tmp}\";" >/dev/null 2>&1 || true
  fi
  psql_admin "ALTER DATABASE \"${pre}\" WITH ALLOW_CONNECTIONS true;" >/dev/null 2>&1 || true
  psql_admin "ALTER DATABASE \"${pre}\" RENAME TO \"${db}\";" >/dev/null 2>&1 || true
  if db_exists "$tmp"; then psql_admin "DROP DATABASE \"${tmp}\";" >/dev/null 2>&1 || true; fi
}

# reconcile_leftover <db> — idempotent crash-recovery for one db, keyed off the
# (<db>, <db>_restore_tmp, <db>_pre_restore) existence triple (the catalog is the
# ground truth; the recorded phase can lag reality). Brings any stranded mid-swap
# state to untouched-original OR completed-restore — never a reachable half-swap.
# Called at the top of restore_one_db_swap (its own db) and as the whole body of
# --recover (the recorded db). It does NOT touch DROP_STARTED/.destructive: the
# maintenance-hold decision is owned by the destructive window (main flow) or the
# durable .destructive sentinel read at --recover startup.
reconcile_leftover() {
  local db="$1"
  # Defensive allowlist: every in-tree caller passes a trusted constant
  # (JARVIS_DB/LITELLM_DB), but this guards the single-quoted SQL sinks below
  # against any future caller — or the --recover path — passing an unvalidated name
  # from the attacker-writable swap-state file into a DROP/RENAME.
  case "$db" in "$JARVIS_DB"|"$LITELLM_DB") ;; *) return 1 ;; esac
  local tmp="${db}_restore_tmp" pre="${db}_pre_restore" is_jarvis=0
  [ "$db" = "$JARVIS_DB" ] && is_jarvis=1
  if ! db_exists "$pre"; then
    # No rollback snapshot -> nothing was renamed for this db; the ORIGINAL <db> is
    # the live data. A stale tmp can exist (aborted reload, or the disallow-before-
    # rename window). Re-enable connections on <db> (no-op if already allowed, but
    # heals the disallow-before-rename crash) and drop the tmp.
    if db_exists "$db"; then psql_admin "ALTER DATABASE \"${db}\" WITH ALLOW_CONNECTIONS true;" >/dev/null 2>&1 || true; fi
    if db_exists "$tmp"; then psql_admin "DROP DATABASE \"${tmp}\";" >/dev/null 2>&1 || true; fi
    clear_swap_state
    return 0
  fi
  # pre_restore EXISTS -> a swap was mid-flight. If <db> is absent but the (already
  # verified) tmp is still there, complete forward by renaming it in.
  if ! db_exists "$db" && db_exists "$tmp"; then
    psql_admin "ALTER DATABASE \"${tmp}\" RENAME TO \"${db}\";" >/dev/null || return 1
  fi
  if db_exists "$db" && verify_db_structural "$db" "$is_jarvis"; then
    psql_admin "DROP DATABASE \"${pre}\";" >/dev/null 2>&1 || true
    if db_exists "$tmp"; then psql_admin "DROP DATABASE \"${tmp}\";" >/dev/null 2>&1 || true; fi
    clear_swap_state
    return 0
  fi
  # <db> missing with no tmp to complete, OR the post-swap verify failed -> REVERT
  # to the untouched pre_restore. The half-swap is resolved either way; clear state.
  revert_swap "$db"
  clear_swap_state
  return 1
}

# restore_one_db_swap <db> <archive> <is_jarvis> — reload the plain-SQL dump into a
# fresh <db>_restore_tmp while the OLD <db> stays LIVE, structurally verify the tmp,
# then atomically swap by rename (disallow -> terminate -> rename-out -> rename-in),
# gate the swapped-in db on a post-swap structural verify, and drop the rollback
# snapshot. The ONLY destructive window is disallow->terminate->rename; a failure
# anywhere before the first rename (bad archive, ENOSPC, timeout, tmp-verify fail)
# leaves production untouched (DROP_STARTED stays 0 -> the EXIT trap lifts the 503).
restore_one_db_swap() {
  local db="$1" archive="$2" is_jarvis="$3"
  local tmp="${db}_restore_tmp" pre="${db}_pre_restore" st
  reconcile_leftover "$db" || return 1
  # (1) reload into a fresh tmp db — writers cannot reach it (unknown name), so the
  #     reload minutes are harmless while OLD <db> keeps serving.
  psql_admin "CREATE DATABASE \"${tmp}\";" >/dev/null || return 1
  write_swap_state "$db" "reload_tmp"
  set +e
  decrypt_or_passthrough "$archive" | gunzip | psql -h "$PGHOST" -U "$PGUSER" -d "$tmp" -v ON_ERROR_STOP=1 -q >/dev/null
  st=("${PIPESTATUS[@]}")
  set -e
  { [ "${st[0]}" -eq 0 ] && [ "${st[1]}" -eq 0 ] && [ "${st[2]}" -eq 0 ]; } || return 1
  # (2) structural verify on the tmp BEFORE any destruction.
  verify_db_structural "$tmp" "$is_jarvis" || return 1
  # ---- destructive window opens HERE (not before): disallow -> terminate -> rename.
  #      Write the DURABLE maintenance marker FIRST and fail-closed: it is the hold
  #      that keeps the stack 503 across a crash mid-swap (.maintenance soft-expires
  #      when the heartbeat dies; .destructive never does). If it cannot be written
  #      we abort before touching anything — the disallow is still below, so nothing
  #      destructive has run and fail_before_destruction lifts maintenance cleanly.
  #      Only after the durable hold is in place do we mark DROP_STARTED (the FE
  #      reads drop_started from the status JSON) and open the window.
  touch "$MAINTENANCE_DESTRUCTIVE" 2>/dev/null \
    || fail_before_destruction "cannot write the durable maintenance marker; refusing to start the destructive swap"
  DROP_STARTED=1
  write_status
  write_swap_state "$db" "swapping_out"
  psql_admin "ALTER DATABASE \"${db}\" WITH ALLOW_CONNECTIONS false;" >/dev/null || return 1
  psql_admin "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${db}' AND pid <> pg_backend_pid();" >/dev/null || return 1
  psql_admin "ALTER DATABASE \"${db}\" RENAME TO \"${pre}\";" >/dev/null || return 1
  write_swap_state "$db" "swapping_in"
  psql_admin "ALTER DATABASE \"${tmp}\" RENAME TO \"${db}\";" >/dev/null || return 1
  write_swap_state "$db" "verified"
  # (3) post-swap structural verify — THE SOLE GATE. On pass, drop the rollback
  #     snapshot; on fail, REVERT to the untouched pre_restore and hold maintenance.
  if verify_db_structural "$db" "$is_jarvis"; then
    psql_admin "DROP DATABASE \"${pre}\";" >/dev/null || return 1
    clear_swap_state
    return 0
  fi
  revert_swap "$db"
  return 1
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
  # --inbox-manifest is a read-only inventory pass: it must NOT consume the restore
  # request, write .restore_status.json, shred the operator key, or touch maintenance.
  # Short-circuit before any of that (it wrote its own manifest and is exiting 0).
  [ "$MANIFEST_MODE" = "1" ] && exit 0
  [ -n "$HEARTBEAT_PID" ] && kill "$HEARTBEAT_PID" 2>/dev/null
  # belt-and-braces: re-consume the request so it can never re-fire. A --recover run
  # never consumes a request (it did not claim one) so a legitimately pending
  # restore that arrives while a crash-recovery is running is not silently eaten.
  [ "$RECOVER_MODE" = "1" ] || rm -f "$REQUEST_FILE" 2>/dev/null
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
  # Any terminal FAILURE that entered the destructive window (DROP_STARTED=1) leaves the
  # DB inconsistent — the operator must recover from the safety backup / finish the
  # off-host secrets+role steps — so report manual_steps_required honestly rather than a
  # bare error. Covers every post-DROP path (step5_fail, STEP-8 fail_after_restore,
  # timeout, abnormal death). A clean restore is STATE="done" here, so it stays false.
  if [ "$STATE" = "failed" ] && [ "$DROP_STARTED" = "1" ]; then MANUAL_STEPS_REQUIRED=1; fi
  [ -n "$FINISHED_AT" ] || FINISHED_AT="$(date -Iseconds)"
  write_status
  # Lift maintenance on ANY clean restore (same-host OR off-host) OR any failure
  # BEFORE the first DROP (DROP_STARTED=0 => nothing was destroyed => safe to serve,
  # true on a fresh host too). A clean INBOX restore is now safe to lift: STEP 8
  # materialized the restored ./secrets and wrote the .secrets_rotated marker, and
  # the app_factory/telegram/litellm watchers self-restart to pick up the rotated
  # secrets + rebound role — so the stack returns to service with no operator steps.
  # A failure AFTER the first DROP still holds the durable .destructive sentinel (the
  # DB is inconsistent; it does NOT auto-expire — the MAINTENANCE_MAX_AGE_S soft-expiry
  # covers .maintenance only — so the operator MUST clear it explicitly). This can't
  # race a concurrent restore: the restore-request POST is itself 503'd while the
  # sentinel is up, so a post-DROP hold is never lifted by a later run.
  if [ "$RESTORE_CLEAN" = "1" ] || [ "$DROP_STARTED" = "0" ]; then
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

# === RECOVERY MODE (--recover): reconcile a stranded mid-swap state, then exit ==
# The sidecar entrypoint invokes this on startup when .restore_swap_state.json is
# present (a crash mid-swap). It runs ONLY the leftover-handler for the recorded db
# — it never consumes a .restore_request.json or runs the full restore flow. A
# durable .destructive sentinel means the crash happened AFTER the destructive
# window opened, so maintenance is held (DROP_STARTED=1) even when the reconcile
# completes the swap: the rest of the restore (Qdrant, the other db) did not run,
# so the operator must re-trigger or clear the sentinels. When nothing was ever
# destroyed (.destructive absent, e.g. a crash during the first reload) the
# reconcile only drops a stale tmp and the EXIT trap lifts the 503.
if [ "${1:-}" = "--recover" ]; then
  RECOVER_MODE=1
  CURRENT_STEP="Recovering an interrupted restore"
  PHASE="recover"
  if [ -f "$MAINTENANCE_DESTRUCTIVE" ]; then DROP_STARTED=1; fi
  RECOVER_DB="$(read_swap_db)"
  if [ -z "$RECOVER_DB" ]; then
    clear_swap_state
    STATE="done"
    FINISHED_AT="$(date -Iseconds)"
    exit 0
  fi
  if [ ! -r /run/secrets/postgres_password ]; then
    STATE="failed"
    ERROR="recovery: cannot read the postgres password secret"
    FINISHED_AT="$(date -Iseconds)"
    exit 0
  fi
  PGPASSWORD="$(cat /run/secrets/postgres_password)"
  export PGPASSWORD
  if reconcile_leftover "$RECOVER_DB"; then
    STATE="done"
  else
    STATE="failed"
    ERROR="could not finish the interrupted restore of ${RECOVER_DB}; the database is consistent (restored or original) but the stack stays in maintenance — re-run the restore or clear the maintenance sentinels per the runbook"
  fi
  FINISHED_AT="$(date -Iseconds)"
  exit 0
fi

# === INVENTORY MODE (--inbox-manifest): refresh the sanitized inbox listing, exit ==
# The sidecar loop invokes this every iteration so the admin app's GET /inbox reflects
# whatever the operator has dropped into the rw restore_inbox. It is READ-ONLY: it
# writes only ${TRIGGER_DIR}/.inbox_manifest.json (names/booleans), never consumes the
# restore request, never touches the DB, and MANIFEST_MODE short-circuits the EXIT trap
# so it cannot shred the operator key or write a restore status. It must never abort the
# loop, so it exits 0 unconditionally.
if [ "${1:-}" = "--inbox-manifest" ]; then
  MANIFEST_MODE=1
  write_inbox_manifest
  exit 0
fi

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
    # Highest NNN_*.sql migration number, via a glob (not `ls`) so filenames are
    # handled safely; 10# forces base-10 so a leading-zero prefix is not read as
    # octal. After a schema squash db/migrations/ is empty (the baseline lives in
    # init.sql), so the glob yields nothing; fall back to db/SCHEMA_VERSION — the
    # single source of the baseline floor — so this gate stays ARMED, not open.
    # An older-than-code backup is NOT held here: it self-heals on the next app
    # recreate (the migration runner forward-migrates it), so only newer-than-code
    # is refused.
    CODE_MAX=""
    for _mig in "$MIG_DIR"/*.sql; do
      [ -e "$_mig" ] || continue
      _num="${_mig##*/}"
      _num="${_num%%_*}"
      case "$_num" in ''|*[!0-9]*) continue ;; esac
      _num=$((10#$_num))
      if [ -z "$CODE_MAX" ] || [ "$_num" -gt "$CODE_MAX" ]; then CODE_MAX="$_num"; fi
    done
    if [ -z "$CODE_MAX" ]; then
      CODE_MAX="$(tr -dc '0-9' < "${SCHEMA_VERSION_FILE:-${MIG_DIR%/migrations}/SCHEMA_VERSION}" 2>/dev/null || true)"
      [ -n "$CODE_MAX" ] || CODE_MAX="$(tr -dc '0-9' < /app/db/SCHEMA_VERSION 2>/dev/null || true)"
      [ -n "$CODE_MAX" ] || CODE_MAX=101
    fi
    if [ -n "$CODE_MAX" ] && [ "$MANIFEST_SCHEMA" -gt "$CODE_MAX" ]; then
      fail_before_destruction "backup is newer than this deployment (schema ${MANIFEST_SCHEMA} > code ${CODE_MAX}); upgrade JARVIS before restoring"
    fi
  fi
elif [ "$SOURCE" = "inbox" ]; then
  # Off-host archives are operator-supplied and less trusted, and the admin endpoint
  # canNOT pre-check their compatibility (the inbox listing carries no schema version).
  # So the manifest is REQUIRED for an inbox restore: without it neither the sha256
  # integrity check above nor the newer-than-code gate can arm. Refuse before any
  # destruction rather than reload an unverified / newer archive set.
  fail_before_destruction "off-host restore requires manifest_${TIMESTAMP}.json (copy the full backup set, including its manifest, into the restore_inbox); nothing was changed"
else
  echo "[restore] WARN: manifest ${MANIFEST} absent; proceeding (local restore — the admin endpoint pre-checks compatibility)" >&2
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

# === STEP 2.5: secrets preflight (inbox only, BEFORE any destruction) =========
# An off-host set with NO secrets archive would otherwise swap BOTH DBs and only fail at
# STEP 8 (secrets materialization) — leaving a shredded one-time key and a durable
# maintenance hold. Verify the secrets archive is present up front and refuse before any
# DROP. STEP 2's manifest sha256 check already integrity-verified it (backup.sh lists the
# secrets archive in the manifest, and an inbox restore hard-requires that manifest), so
# presence here means present + integrity-checked. Local restores never touch secrets.
if [ "$SOURCE" = "inbox" ]; then
  if ! resolve_secrets_archive >/dev/null; then
    MANUAL_STEPS_REQUIRED=1
    fail_before_destruction "off-host restore requires the secrets archive secrets_${TIMESTAMP}.tar.gz[.enc] (stage the full backup set, including its secrets, into the restore_inbox); nothing was changed"
  fi
fi

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
# RETENTION_DAYS, and the STEP-5 reload reads that archive.
export BACKUP_SKIP_PRUNE=1
# Force the safety backup to run even though .maintenance is already up: the backup
# script's own maintenance skip-guard would otherwise treat the restore's own
# maintenance sentinel as "someone else is mid-restore" and abort the very safety
# snapshot the restore depends on.
export BACKUP_FORCE=1
if /usr/local/bin/backup.sh; then SAFETY_RC=0; else SAFETY_RC=$?; echo "[restore] WARN: safety backup.sh exited non-zero (${SAFETY_RC})" >&2; fi
LAST_RUN="$(cat "${BACKUP_DIR}/.last_run.json" 2>/dev/null || true)"
SAFETY_BACKUP_TS="$(printf '%s' "$LAST_RUN" \
  | grep -oE '"timestamp"[[:space:]]*:[[:space:]]*"[0-9]{8}_[0-9]{6}"' | grep -oE '[0-9]{8}_[0-9]{6}' | head -1 || true)"
# The safety pre-backup is the ONLY rollback point for a mid-swap failure, so it must be
# proven FRESH for THIS run (exit 0 + succeeded + newer than STARTED_AT), not merely
# "some prior backup succeeded". A failed backup that could not rewrite .last_run.json
# would leave a stale succeeded record; treating that as a rollback point is the bug this guards against.
if ! safety_backup_is_fresh "$SAFETY_RC"; then
  STEP_SAFETY="failed"
  MANUAL_STEPS_REQUIRED=1
  fail_before_destruction "safety backup failed or is stale (no fresh rollback point); nothing was changed"
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

# === STEP 4.5: disk preflight (before the first tmp CREATE) ==================
# The rename-swap keeps both live DBs while it reloads a transient tmp DB per DB,
# so refuse fast if the volume cannot hold live + tmp + headroom (fail_before_-
# destruction lifts maintenance; nothing was touched).
preflight_disk_or_fail

# === STEP 5: restore the DBs — the rename-swap holds the ONLY destructive window
# Each DB is reloaded into <db>_restore_tmp (non-destructive; OLD <db> stays live),
# then swapped in by rename; the destructive window is only disallow->terminate->
# rename. A failure before the first rename leaves production untouched.
CURRENT_STEP="Restoring database"
PHASE="reload-db"
STEP_DB="running"
write_status
if restore_one_db_swap "$JARVIS_DB" "$JARVIS_ARCHIVE" 1; then
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
if restore_one_db_swap "$LITELLM_DB" "$LITELLM_ARCHIVE" 0; then
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
  SECRETS_ARCHIVE="$(resolve_secrets_archive || true)"
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
  # Reject symlink/hardlink members too (verbose listing: type char 'l'/'h'). A
  # legitimate secrets archive is flat regular *.txt files; a crafted symlink member
  # (e.g. d -> /host-secrets, now a rw mount) could otherwise redirect a subsequent
  # member's write outside the staging dir during extraction.
  SECRETS_TAR_TYPES="$(tar -tvzf "$SECRETS_TAR_TMP" 2>/dev/null || true)"
  if grep -Eq '^[lh]' <<<"$SECRETS_TAR_TYPES"; then
    fail_after_restore "off-host restore: the secrets archive contains a symlink or hardlink member; refusing to extract — see the runbook"
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
  # Materialize the restored secrets into the host ./secrets (bind-mounted here rw as
  # HOST_SECRETS_DIR) so the app containers self-restart onto them — a fresh host now
  # recovers with ZERO terminal steps. Runs BEFORE the EXIT trap purges the staging
  # tree. The tar was traversal-checked at extraction (:951); re-assert a strict flat
  # basename (defense-in-depth) and skip credentials the TARGET host is authoritative
  # for — backup_encrypt_key.txt (already excluded from the archive) plus the local-
  # service keys handled in the loop below. Per-file atomic
  # tmp->mv. Mode 0644 matches setup.sh's convention (files 0644 inside a 0700 dir):
  # the sidecar writes as root but the app containers read /run/secrets/* as a
  # DIFFERENT non-root uid via bind mount, so a 0600 file would be unreadable and the
  # self-restart would fail to reconnect. Any failure holds maintenance.
  if [ ! -d "$HOST_SECRETS_DIR" ]; then
    fail_after_restore "off-host restore: ${HOST_SECRETS_DIR} is not mounted; cannot materialize the restored secrets — recreate the app containers manually per the runbook"
  fi
  shopt -s nullglob
  for sfile in "${SECRETS_STAGING}"/*.txt; do
    sbase="$(basename "$sfile")"
    [ "$sbase" = "backup_encrypt_key.txt" ] && continue
    # qdrant_api_key / langfuse_pg_password are LOCAL access credentials to LOCAL
    # services (qdrant; the langfuse-postgres backing store), not restored user data.
    # Those services fixed their own key at first-init on the target host and are NOT
    # restarted by a restore (no backup_trigger mount, no .secrets_rotated watcher,
    # and unlike PGUSER no ALTER ROLE rebind), so the app must keep the TARGET host's
    # copy to stay authenticated — materializing the OLD key would break app<->qdrant
    # and langfuse<->langfuse-postgres auth on the next restart of those services.
    [ "$sbase" = "qdrant_api_key.txt" ] && continue
    [ "$sbase" = "langfuse_pg_password.txt" ] && continue
    printf '%s' "$sbase" | grep -Eq '^[a-z0-9_]+\.txt$' || continue
    if ! cp -- "$sfile" "${HOST_SECRETS_DIR}/${sbase}.tmp" 2>/dev/null \
       || ! chmod 644 "${HOST_SECRETS_DIR}/${sbase}.tmp" 2>/dev/null \
       || ! mv -f "${HOST_SECRETS_DIR}/${sbase}.tmp" "${HOST_SECRETS_DIR}/${sbase}" 2>/dev/null; then
      rm -f "${HOST_SECRETS_DIR}/${sbase}.tmp" 2>/dev/null || true
      shopt -u nullglob
      fail_after_restore "off-host restore: could not materialize secret ${sbase} into ${HOST_SECRETS_DIR}; databases were restored — recreate the app containers manually per the runbook"
    fi
  done
  shopt -u nullglob
  # Rotation marker (integer epoch): each postgres-connecting service restarts iff
  # this marker is newer than ITS process start, so they come back reading the rotated
  # secrets + rebound role (self-limiting — no restart loop). Atomic tmp->mv.
  if ! { printf '%s\n' "$(date +%s)" > "${TRIGGER_DIR}/.secrets_rotated.tmp" \
         && mv -f "${TRIGGER_DIR}/.secrets_rotated.tmp" "${TRIGGER_DIR}/.secrets_rotated"; }; then
    rm -f "${TRIGGER_DIR}/.secrets_rotated.tmp" 2>/dev/null || true
    fail_after_restore "off-host restore: could not write the secrets-rotation marker; databases were restored — recreate the app containers manually per the runbook"
  fi
  write_status
fi

# === STEP 9: finishing up ====================================================
# A clean restore — same-host OR off-host — lifts maintenance via the EXIT trap.
# The off-host path now materializes the restored ./secrets and writes the rotation
# marker in STEP 8, and the app_factory/telegram/litellm watchers self-restart to
# read the rotated secrets + rebound role, so the stack returns to service with no
# terminal steps. MANUAL_STEPS_REQUIRED stays 0 (reserved for genuinely
# unrecoverable states) so write_status reports honestly.
CURRENT_STEP="Finishing up"
PHASE="finalize"
STEP_FINISH="running"
write_status
RESTORE_CLEAN=1
STATE="done"
# An older-than-code backup is no longer held here: it self-heals on the next app
# recreate (the migration runner forward-migrates it).
STEP_FINISH="done"
FINISHED_AT="$(date -Iseconds)"
write_status
if [ "$SOURCE" = "inbox" ]; then
  echo "[restore] off-host restore complete: databases + vectors + secrets restored, the postgres role rebound, and the app services self-restarting to reload the rotated secrets. The stack returns to service automatically." >&2
fi
# The EXIT trap clears both .maintenance and .destructive on any clean restore
# + kills the heartbeat.
