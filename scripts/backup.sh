#!/usr/bin/env bash
# backup.sh — JARVIS PostgreSQL backup sidecar
#
# What it backs up:
#   - PostgreSQL dump (pg_dump | gzip → /backups/jarvis_<timestamp>.sql.gz)
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
#   When set and the file is non-empty, output is piped through
#   openssl enc -aes-256-cbc -pbkdf2 and saved as .sql.gz.enc instead of .sql.gz.
#
# Decryption recipe:
#   openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -d -kfile "$BACKUP_ENCRYPT_KEYFILE" \
#       -in jarvis_<timestamp>.sql.gz.enc | gunzip > backup.sql
#
# Docker Compose: run under --profile backup (service: postgres-backup).
# Env vars: BACKUP_S3_BUCKET, BACKUP_RETENTION_DAYS, BACKUP_INTERVAL_SECONDS,
#           BACKUP_ENCRYPT_KEYFILE
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
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting backup..."
# Write to a temp file and only promote it on full-pipeline success (set -o
# pipefail + the mv below), so a mid-stream pg_dump/gzip/openssl failure can
# never leave a truncated archive that looks like a valid backup.
if [ -n "$ENC_KEYFILE" ] && [ -s "$ENC_KEYFILE" ]; then
  BACKUP_FILE="${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz.enc"
else
  BACKUP_FILE="${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz"
fi
TMP_FILE="${BACKUP_FILE}.tmp"
trap 'rm -f "${TMP_FILE:-}"' EXIT

if [ -n "$ENC_KEYFILE" ] && [ -s "$ENC_KEYFILE" ]; then
  pg_dump -h "${PGHOST:-postgres}" -U "${PGUSER:-jarvis}" -d "${PGDATABASE:-jarvis}" \
    --no-owner --no-acl \
    | gzip \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -kfile "$ENC_KEYFILE" > "$TMP_FILE"
else
  pg_dump -h "${PGHOST:-postgres}" -U "${PGUSER:-jarvis}" -d "${PGDATABASE:-jarvis}" \
    --no-owner --no-acl | gzip > "$TMP_FILE"
fi
# Restrict archive permissions so only root/owner can read it, then atomically
# promote the completed temp file to its final name.
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$BACKUP_FILE"
echo "[$(date -Iseconds)] Backup saved to $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

# Optional S3 upload
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "[$(date -Iseconds)] aws CLI not available; skipping S3 upload (install awscli or use the amazon/aws-cli sidecar)"
  else
    aws s3 cp "$BACKUP_FILE" "s3://${BACKUP_S3_BUCKET}/$(basename "$BACKUP_FILE")"
    echo "[$(date -Iseconds)] Uploaded to s3://${BACKUP_S3_BUCKET}/"
  fi
fi

# Prune old backups (both plain and encrypted archives)
find "$BACKUP_DIR" \( -name "jarvis_*.sql.gz" -o -name "jarvis_*.sql.gz.enc" \) \
  -mtime "+${RETENTION_DAYS}" -delete
echo "[$(date -Iseconds)] Pruned backups older than ${RETENTION_DAYS} days"
