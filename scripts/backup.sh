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
# Docker Compose: run under --profile backup (service: postgres-backup).
# Env vars: BACKUP_S3_BUCKET, BACKUP_RETENTION_DAYS, BACKUP_INTERVAL_SECONDS
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
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting backup..."
pg_dump -h "${PGHOST:-postgres}" -U "${PGUSER:-jarvis}" -d "${PGDATABASE:-jarvis}" \
  --no-owner --no-acl | gzip > "$BACKUP_FILE"
# Restrict archive permissions so only root/owner can read it.
# For at-rest encryption, pipe through openssl enc -aes-256-cbc -kfile <secret>
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

# Prune old backups
find "$BACKUP_DIR" -name "jarvis_*.sql.gz" -mtime "+${RETENTION_DAYS}" -delete
echo "[$(date -Iseconds)] Pruned backups older than ${RETENTION_DAYS} days"
