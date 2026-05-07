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
#   openssl enc -aes-256-cbc -pbkdf2 -d -kfile "$BACKUP_ENCRYPT_KEYFILE" \
#       -in jarvis_<timestamp>.sql.gz.enc | gunzip > backup.sql
#
# Docker Compose: run under --profile backup (service: postgres-backup).
# Env vars: BACKUP_S3_BUCKET, BACKUP_RETENTION_DAYS, BACKUP_INTERVAL_SECONDS,
#           BACKUP_ENCRYPT_KEYFILE
#   — see .env.example for defaults.

set -euo pipefail

# Read PGPASSWORD from Docker Secret (preferred) or fall back to env var.
if [ -r /run/secrets/postgres_password ]; then
  export PGPASSWORD
  PGPASSWORD="$(cat /run/secrets/postgres_password)"
fi

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
ENC_KEYFILE="${BACKUP_ENCRYPT_KEYFILE:-}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting backup..."
if [ -n "$ENC_KEYFILE" ] && [ -s "$ENC_KEYFILE" ]; then
  BACKUP_FILE="${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz.enc"
  pg_dump -h "${PGHOST:-postgres}" -U "${PGUSER:-jarvis}" -d "${PGDATABASE:-jarvis}" \
    --no-owner --no-acl \
    | gzip \
    | openssl enc -aes-256-cbc -pbkdf2 -kfile "$ENC_KEYFILE" > "$BACKUP_FILE"
  echo "[$(date -Iseconds)] Encrypted backup saved to $BACKUP_FILE"
else
  BACKUP_FILE="${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz"
  pg_dump -h "${PGHOST:-postgres}" -U "${PGUSER:-jarvis}" -d "${PGDATABASE:-jarvis}" \
    --no-owner --no-acl | gzip > "$BACKUP_FILE"
fi
# Restrict archive permissions so only root/owner can read it.
chmod 600 "$BACKUP_FILE"
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
