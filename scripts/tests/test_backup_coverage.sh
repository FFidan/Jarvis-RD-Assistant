#!/usr/bin/env bash
# test_backup_coverage.sh — assert scripts/backup.sh covers the full DR surface.
#
# JARVIS state lives in three places, not one: the jarvis Postgres DB, the
# litellm Postgres DB (API keys / spend / virtual keys), the Qdrant vector
# store, and the on-disk secrets/ keys (without which an encrypted backup is
# undecryptable). This test guards that backup.sh references all three of the
# coverage additions beyond the original jarvis-only dump.
#
# Run: bash scripts/tests/test_backup_coverage.sh   (exit 0 = pass)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="${SCRIPT_DIR}/../backup.sh"

fail=0
pass() { printf 'PASS: %s\n' "$1"; }
check() {
  # check <human description> <grep -E pattern>
  if grep -Eq "$2" "$BACKUP_SCRIPT"; then
    pass "$1"
  else
    printf 'FAIL: %s (pattern: %s)\n' "$1" "$2" >&2
    fail=1
  fi
}

if [ ! -r "$BACKUP_SCRIPT" ]; then
  printf 'FAIL: cannot read %s\n' "$BACKUP_SCRIPT" >&2
  exit 1
fi

# 1. The litellm database is dumped (a literal `litellm` DB reference).
check "backs up the litellm database" '\blitellm\b'

# 2. The Qdrant snapshot REST endpoint is invoked.
check "creates a Qdrant snapshot via the snapshots endpoint" '/collections/[^ ]*/snapshots'

# 3. The on-disk secrets/ directory is archived (a tar of the secrets source
#    dir — distinct from the /run/secrets/* docker-secret mounts the script
#    already reads).
check "archives the secrets/ directory" 'tar.*\$\{?SECRETS_DIR|SECRETS_DIR=.*/secrets'

# 4. The original jarvis-DB dump + its encryption path are NOT removed.
check "still dumps the jarvis database" 'pg_dump.*jarvis|PGDATABASE'
check "still supports openssl at-rest encryption" 'openssl enc -aes-256-cbc'

# 5. Qdrant failures must be non-fatal (best-effort), so an offline Qdrant
#    cannot abort the Postgres/secrets backups.
check "treats Qdrant snapshot as optional/non-fatal" 'optional|non-fatal|best-effort|continue|\|\| *(true|echo)'

# 6. BAC-1: the backup encryption key file must be excluded from the secrets
#    archive (circular-undecryptability guard after total host loss). The exclude
#    must name the real file backup_encrypt_key.txt — a keyless pattern is a no-op.
check "excludes the backup key from the secrets archive" '\-\-exclude=\./backup_encrypt_key\.txt'

# 7. BAC-3: the Qdrant snapshot download must stream to the filehandle via a
#    data_callback, not buffer the whole body in $res->{content}.
check "streams the Qdrant snapshot via data_callback" 'data_callback'

# 8. BAC-4: the retention prune must also remove orphaned .tmp staging files.
check "prunes orphaned .tmp staging files" '\-name .\*\.tmp'

# 9. BAC-1 (behavioral): replay backup.sh's OWN exclude flag against a fixture and
#    prove it actually drops backup_encrypt_key.txt while keeping other secrets.
#    Source-grep alone can't catch a keyless no-op pattern (GNU tar globs member
#    names, so ./backup_encrypt_key would silently fail to match the .txt file).
bac1_excl="$(grep -oE '\-\-exclude=[^ ]*backup_encrypt_key[^ ]*' "$BACKUP_SCRIPT" | head -1)"
bac1_dir="$(mktemp -d)"
printf 'KEY' > "$bac1_dir/backup_encrypt_key.txt"
printf 'PW' > "$bac1_dir/postgres_password.txt"
if [ -n "$bac1_excl" ] \
   && ! tar -czf - -C "$bac1_dir" "$bac1_excl" . | tar -tzf - | grep -q 'backup_encrypt_key' \
   && tar -czf - -C "$bac1_dir" "$bac1_excl" . | tar -tzf - | grep -q 'postgres_password'; then
  pass "secrets-tar exclude actually drops backup_encrypt_key.txt (keeps other secrets)"
else
  printf 'FAIL: secrets-tar exclude (%s) does not drop backup_encrypt_key.txt\n' "$bac1_excl" >&2
  fail=1
fi
rm -rf "$bac1_dir"

# N. On-demand trigger: backup.sh consumes a sentinel flag-file (written by the
#    WebUI Backup panel) so the sidecar runs immediately, then clears it.
check "consumes the on-demand backup-trigger sentinel" 'backup_now'

# N+1. The app service mounts the backups volume read-only for list/download.
COMPOSE="${SCRIPT_DIR}/../../docker-compose.yml"
if grep -Eq 'postgres_backups:/backups:ro' "$COMPOSE"; then
  pass "paper_ingestion mounts postgres_backups read-only"
else
  printf 'FAIL: paper_ingestion missing postgres_backups:/backups:ro\n' >&2
  fail=1
fi

# N+2. A shared RW trigger volume is mounted into BOTH app and sidecar.
if [ "$(grep -c 'backup_trigger:/backup-trigger' "$COMPOSE")" -ge 2 ]; then
  pass "backup_trigger volume shared between app and sidecar"
else
  printf 'FAIL: backup_trigger volume not mounted into both services\n' >&2
  fail=1
fi

# N+3. The app image chowns the trigger mount point to appuser BEFORE dropping
#      privileges — otherwise the non-root sentinel write 503s in production.
DOCKERFILE="${SCRIPT_DIR}/../../services/paper_ingestion/Dockerfile"
if grep -Eq 'chown[[:space:]]+appuser:appuser[[:space:]]+/backup-trigger' "$DOCKERFILE" \
   && awk '/chown[[:space:]]+appuser:appuser[[:space:]]+\/backup-trigger/{c=NR} /^USER appuser/{u=NR} END{exit !(c && u && c<u)}' "$DOCKERFILE"; then
  pass "Dockerfile chowns /backup-trigger to appuser before USER appuser"
else
  printf 'FAIL: Dockerfile missing /backup-trigger chown before USER appuser (non-root trigger write will 503)\n' >&2
  fail=1
fi

# N+4. The backup sidecar must run BY DEFAULT — DR is core and archives are
#      encrypted with the auto-generated key, so it must NOT be hidden behind the
#      opt-in `backup` compose profile. Guard against a regression re-gating it in
#      EITHER inline (`profiles: [backup]`) or block (`profiles:` / `  - backup`)
#      form: assert the postgres-backup stanza carries no `profiles:` key at all.
if awk '/^  postgres-backup:/{f=1; next} /^  [a-zA-Z]/{f=0} f && /^[[:space:]]*profiles:/{found=1} END{exit !found}' "$COMPOSE"; then
  printf 'FAIL: postgres-backup has a profiles: key — backups must run by default (not profile-gated)\n' >&2
  fail=1
else
  pass "backup sidecar runs by default (postgres-backup has no profiles: gate)"
fi

if [ "$fail" -ne 0 ]; then
  printf '\nbackup coverage: FAILED\n' >&2
  exit 1
fi
printf '\nbackup coverage: all checks passed\n'
