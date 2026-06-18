#!/usr/bin/env bash
# backup.sh — JARVIS disaster-recovery backup sidecar
#
# What it backs up (JARVIS state lives in more than one place):
#   - PostgreSQL `jarvis` DB  → /backups/jarvis_<timestamp>.sql.gz[.enc]
#   - PostgreSQL `litellm` DB → /backups/litellm_<timestamp>.sql.gz[.enc]
#       (API keys, virtual keys, spend ledger; owned by the same superuser that
#        ran createdb, so the PGUSER dump has rights)
#   - Qdrant vector store     → /backups/qdrant_<collection>_<timestamp>.snapshot
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
# Docker Compose: run under --profile backup (service: postgres-backup).
# Env vars: BACKUP_S3_BUCKET, BACKUP_RETENTION_DAYS, BACKUP_INTERVAL_SECONDS,
#           BACKUP_ENCRYPT_KEYFILE, QDRANT_URL, LITELLM_DATABASE, SECRETS_DIR
#   — see .env.example for defaults.

set -euo pipefail

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
LITELLM_BACKUP_FILE="$(dump_db "$LITELLM_DATABASE" litellm)"

# --- secrets/ directory ------------------------------------------------------
# The Docker-secret source files; an encrypted DB backup is useless without the
# key that decrypts it. Always encrypted when a backup key is configured.
SECRETS_BACKUP_FILE=""
if [ -d "$SECRETS_DIR" ]; then
  if [ "$ENCRYPT" -eq 1 ]; then
    SECRETS_BACKUP_FILE="${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz.enc"
  else
    SECRETS_BACKUP_FILE="${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz"
    echo "[$(date -Iseconds)] WARNING: BACKUP_ENCRYPT_KEYFILE is unset — the secrets archive will contain plaintext keys. Set a backup key for production." >&2
  fi
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
    echo "FATAL: secrets archive failed (tar=${secrets_st[0]} enc=${secrets_st[1]})" >&2
    exit 1
  fi
  chmod 600 "$secrets_tmp"
  mv "$secrets_tmp" "$SECRETS_BACKUP_FILE"
  echo "[$(date -Iseconds)] Backup saved to $SECRETS_BACKUP_FILE ($(du -h "$SECRETS_BACKUP_FILE" | cut -f1))"
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
        qsnap_tmp="${BACKUP_DIR}/qdrant_${col}_${TIMESTAMP}.snapshot.tmp"
        qsnap_out="${BACKUP_DIR}/qdrant_${col}_${TIMESTAMP}.snapshot"
        if qdrant_http GET "/collections/${col}/snapshots/${snap}" "$qsnap_tmp"; then
          chmod 600 "$qsnap_tmp"; mv "$qsnap_tmp" "$qsnap_out"
          echo "[$(date -Iseconds)] Qdrant snapshot saved to $qsnap_out ($(du -h "$qsnap_out" | cut -f1))"
        else
          rm -f "$qsnap_tmp"
          echo "[$(date -Iseconds)] WARNING: failed to download Qdrant snapshot for '$col'; continuing" >&2
        fi
      else
        echo "[$(date -Iseconds)] WARNING: could not parse Qdrant snapshot name for '$col'; continuing" >&2
      fi
    else
      echo "[$(date -Iseconds)] WARNING: failed to create Qdrant snapshot for '$col'; continuing" >&2
    fi
  done
else
  echo "[$(date -Iseconds)] Qdrant unreachable at $QDRANT_URL; skipping vector snapshot (Postgres/secrets backups unaffected)" >&2
fi

# --- Optional S3 upload ------------------------------------------------------
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "[$(date -Iseconds)] aws CLI not available; skipping S3 upload (install awscli or use the amazon/aws-cli sidecar)"
  else
    for f in "$JARVIS_BACKUP_FILE" "$LITELLM_BACKUP_FILE" "$SECRETS_BACKUP_FILE" \
             "$BACKUP_DIR"/qdrant_*_"${TIMESTAMP}".snapshot; do
      [ -n "$f" ] && [ -f "$f" ] || continue
      aws s3 cp "$f" "s3://${BACKUP_S3_BUCKET}/$(basename "$f")"
    done
    echo "[$(date -Iseconds)] Uploaded backups to s3://${BACKUP_S3_BUCKET}/"
  fi
fi

# --- Prune old backups (DB dumps, secrets archives, Qdrant snapshots) --------
find "$BACKUP_DIR" \( \
    -name "jarvis_*.sql.gz" -o -name "jarvis_*.sql.gz.enc" \
    -o -name "litellm_*.sql.gz" -o -name "litellm_*.sql.gz.enc" \
    -o -name "secrets_*.tar.gz" -o -name "secrets_*.tar.gz.enc" \
    -o -name "qdrant_*.snapshot" \
    -o -name "*.tmp" \
  \) -mtime "+${RETENTION_DAYS}" -delete
echo "[$(date -Iseconds)] Pruned backups older than ${RETENTION_DAYS} days"
