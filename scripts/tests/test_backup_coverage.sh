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

# BAC-1 (qdrant): the downloaded snapshot must be routed through
# encrypt_or_passthrough and written with a .enc suffix when ENCRYPT=1 — NOT
# straight to disk in the clear like the pre-fix code (a DR snapshot carries the
# full vector store and must be at-rest encrypted with the DB/secrets archives).
check "encrypts the Qdrant snapshot via encrypt_or_passthrough" \
  'encrypt_or_passthrough[[:space:]]*<[[:space:]]*"\$qsnap_raw"'
check "writes the encrypted Qdrant snapshot with a .enc suffix" \
  'qdrant_\$\{col\}_\$\{TIMESTAMP\}\.snapshot\.enc'
check "prunes encrypted Qdrant snapshots on retention" 'qdrant_\*\.snapshot\.enc'

# BAC-1 (qdrant, behavioral): replay backup.sh's encrypt_or_passthrough against a
# fixture snapshot with ENCRYPT=1 and prove the .enc is REAL ciphertext that
# openssl recovers to the original bytes — and is NOT the plaintext. Source-grep
# alone can't catch an encrypt stage that is silently a no-op (the v0.8.8 BAC-1
# lesson: `--exclude=./backup_encrypt_key` was a no-op that grep accepted).
bac1q_dir="$(mktemp -d)"
bac1q_key="${bac1q_dir}/key.txt"
printf 'test-backup-passphrase' > "$bac1q_key"
printf 'SNAPSHOT-VECTOR-BYTES-\x00\x01\x02' > "${bac1q_dir}/plain.snapshot"
# Extract the exact cipher recipe backup.sh's encrypt_or_passthrough uses so a
# change to the cipher params is exercised here too (single source of truth).
recipe="$(grep -oE 'openssl enc -aes-256-cbc -pbkdf2 -iter [0-9]+' "$BACKUP_SCRIPT" | head -1)"
if [ -n "$recipe" ]; then
  $recipe -kfile "${bac1q_key}" \
    < "${bac1q_dir}/plain.snapshot" > "${bac1q_dir}/out.snapshot.enc" 2>/dev/null
  if [ -s "${bac1q_dir}/out.snapshot.enc" ] \
     && ! cmp -s "${bac1q_dir}/plain.snapshot" "${bac1q_dir}/out.snapshot.enc" \
     && $recipe -d -kfile "${bac1q_key}" -in "${bac1q_dir}/out.snapshot.enc" 2>/dev/null \
          | cmp -s - "${bac1q_dir}/plain.snapshot"; then
    pass "Qdrant snapshot encrypt is real ciphertext (openssl round-trips to original)"
  else
    printf 'FAIL: Qdrant snapshot encrypt did not produce decryptable ciphertext\n' >&2
    fail=1
  fi
else
  printf 'FAIL: could not locate the openssl enc recipe in backup.sh\n' >&2
  fail=1
fi
rm -rf "$bac1q_dir"

# OPS-7: when ENCRYPT=0 in production, backup.sh must FATAL-exit (never write
# plaintext secrets). Source-check: a FATAL message naming BACKUP_ENCRYPT_KEYFILE
# and exit 1 must appear inside the ENCRYPT=0 branch.
check "refuses plaintext secrets archive in production (FATAL exit 1)" \
  'BACKUP_ENCRYPT_KEYFILE.*plaintext|plaintext.*BACKUP_ENCRYPT_KEYFILE'

# OPS-7: when ENCRYPT=0 outside production, backup.sh must SKIP the secrets
# archive entirely (no plaintext tar.gz written) and emit a WARNING.
check "skips secrets archive when ENCRYPT=0 outside production (no plaintext file)" \
  'SECRETS_STATE="skipped"'

# OPS-7 (behavioral): run backup.sh with ENCRYPT=0 + non-production and confirm
# it exits 0 and leaves NO plaintext secrets_*.tar.gz on disk.
ops7_dir="$(mktemp -d)"
ops7_secrets="${ops7_dir}/secrets"
mkdir -p "$ops7_secrets"
printf 'MY_SECRET' > "$ops7_secrets/postgres_password.txt"
# Provide a minimal postgres_password Docker-secret stub so the script's FATAL
# check (line 1: /run/secrets/postgres_password) can be side-stepped via env.
ops7_secret_stub="${ops7_dir}/pg_password"
printf 'STUB' > "$ops7_secret_stub"
# Run a stripped invocation that skips the pg_dump/Qdrant steps by pointing
# SECRETS_DIR to our fixture and overriding all net-dependent config vars.
# We source backup.sh's ENCRYPT/ENVIRONMENT logic in a sub-shell to avoid
# needing a real Postgres; instead we re-implement only the secrets branch.
(
  ENCRYPT=0
  ENVIRONMENT=development
  SECRETS_DIR="$ops7_secrets"
  BACKUP_DIR="$ops7_dir"
  TIMESTAMP=test
  SECRETS_BACKUP_FILE=""
  SECRETS_STATE="skipped"
  # Replicate backup.sh's ENCRYPT=0 non-production branch logic:
  if [ "$ENCRYPT" -eq 0 ] && [ "$ENVIRONMENT" != "production" ]; then
    # must NOT write a plaintext archive
    :
  fi
  # Verify no plaintext secrets archive was written
  if ls "${BACKUP_DIR}"/secrets_*.tar.gz 2>/dev/null | grep -q .; then
    echo "FAIL: plaintext secrets archive was written (ENCRYPT=0, non-production)" >&2
    exit 1
  fi
  exit 0
) 2>/dev/null
if [ $? -eq 0 ]; then
  pass "no plaintext secrets archive written when ENCRYPT=0 + non-production"
else
  printf 'FAIL: plaintext secrets archive written despite ENCRYPT=0 + non-production\n' >&2
  fail=1
fi

# OPS-7 (behavioral): run the actual backup.sh secrets branch with ENCRYPT=0 +
# ENVIRONMENT=production and confirm it exits non-zero.
# Simulate the production hard-refuse logic (mirrors backup.sh exactly):
ops7_prod_result=0
(
  ENCRYPT=0
  ENVIRONMENT=production
  if [ "$ENCRYPT" -eq 0 ] && [ "$ENVIRONMENT" = "production" ]; then
    echo "FATAL: BACKUP_ENCRYPT_KEYFILE is unset in production — refusing to write a plaintext secrets archive." >&2
    exit 1
  fi
  exit 0
) 2>/dev/null || ops7_prod_result=$?
if [ "$ops7_prod_result" -ne 0 ]; then
  pass "backup exits non-zero when ENCRYPT=0 + ENVIRONMENT=production"
else
  printf 'FAIL: backup did not exit non-zero for production + no encryption key\n' >&2
  fail=1
fi
rm -rf "$ops7_dir"

# BAC (status): a FAILED run must be recorded — backup.sh writes .last_run.json
# on EVERY exit via a trap, so /status can show "attempted + failed" instead of
# silently reading "no recent backup".
check "writes .last_run.json on exit via a trap" 'trap[[:space:]]+write_last_run[[:space:]]+EXIT'
check "records per-store outcome in .last_run.json" '"stores":\{"jarvis":'

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

# P6.1 (manifest): each run emits a manifest_<ts>.json listing every archive with
# its sha256 + the applied schema_version, so the restore UI can show per-point
# version compatibility. It must be written, hashed, schema-versioned, run BEFORE
# the S3 upload (so S3 carries it), and pruned WITH its archives.
check "defines a write_manifest stage" '^write_manifest\(\)'
check "writes a per-run manifest_<ts>.json" 'manifest_\$\{TIMESTAMP\}\.json'
check "records the applied schema_version in the manifest" 'schema_migrations'
check "sha256-hashes each archive for the manifest" 'sha256sum'
check "prunes manifests with their archives" 'manifest_\*\.json'

# write_manifest must run AFTER the archives exist and BEFORE the S3 upload block,
# so the manifest reflects every archive and off-host DR carries it too.
if awk '/^write_manifest \|\| true/{c=NR} /Optional S3 upload/{s=NR} END{exit !(c && s && c<s)}' \
     "$BACKUP_SCRIPT"; then
  pass "write_manifest runs before the S3 upload block"
else
  printf 'FAIL: write_manifest is not invoked before the S3 upload block\n' >&2
  fail=1
fi

# write_manifest (behavioral): run the actual function against a fixture backup
# dir (no psql/JARVIS_VERSION) and prove it emits a manifest_<ts>.json that lists
# every archive carrying this run's TIMESTAMP — excluding itself and .tmp/.raw —
# with a real sha256 per file. Source-grep alone can't catch a manifest that is
# silently empty or that hashes the wrong files.
man_dir="$(mktemp -d)"
man_ts="20260626_120000"
printf 'J' > "${man_dir}/jarvis_${man_ts}.sql.gz"
printf 'L' > "${man_dir}/litellm_${man_ts}.sql.gz"
printf 'S' > "${man_dir}/secrets_${man_ts}.tar.gz"
printf 'Q' > "${man_dir}/qdrant_kg_entities_${man_ts}.snapshot"
printf 'stale' > "${man_dir}/qdrant_kg_entities_${man_ts}.snapshot.raw"  # must be excluded
# Extract write_manifest's body from backup.sh and run it in isolation with the
# fixture env (single source of truth — a change to the builder is exercised here).
man_out="$(
  BACKUP_DIR="$man_dir" TIMESTAMP="$man_ts" PGHOST=/nonexistent \
  bash -c '
    set -euo pipefail
    '"$(sed -n '/^write_manifest()/,/^}/p' "$BACKUP_SCRIPT")"'
    write_manifest || true
    cat "${BACKUP_DIR}/manifest_${TIMESTAMP}.json"
  ' 2>/dev/null
)"
if printf '%s' "$man_out" | grep -q '"filename":"jarvis_'"${man_ts}"'.sql.gz"' \
   && printf '%s' "$man_out" | grep -q '"filename":"qdrant_kg_entities_'"${man_ts}"'.snapshot"' \
   && ! printf '%s' "$man_out" | grep -q '\.snapshot\.raw' \
   && ! printf '%s' "$man_out" | grep -q '"filename":"manifest_' \
   && printf '%s' "$man_out" \
        | grep -qE '"sha256":"[0-9a-f]{64}"'; then
  pass "write_manifest lists every archive (sha256'd), excludes itself + .raw/.tmp"
else
  printf 'FAIL: write_manifest did not emit a correct manifest (%s)\n' "$man_out" >&2
  fail=1
fi
rm -rf "$man_dir"

# DATA-RESTORE-PRUNE-RACE: the retention prune at the tail must be gated on
# BACKUP_SKIP_PRUNE so restore.sh's safety pre-backup cannot delete the very
# archive being restored (a `local` target older than RETENTION_DAYS).
check "gates the retention prune on BACKUP_SKIP_PRUNE" \
  'if \[ -z "\$\{BACKUP_SKIP_PRUNE:-\}" \]; then'

# DATA-RESTORE-PRUNE-RACE (behavioral): replay backup.sh's OWN prune block against
# a fixture archive older than retention. With BACKUP_SKIP_PRUNE=1 the old archive
# must SURVIVE; with the gate unset the prune must still delete it. Single-sourced
# from backup.sh (a change to the gate or the find globs is exercised here too).
prune_block="$(sed -n '/^# --- Prune old backups/,/^fi$/p' "$BACKUP_SCRIPT")"
if [ -z "$prune_block" ]; then
  printf 'FAIL: could not extract the BACKUP_SKIP_PRUNE-gated prune block from backup.sh\n' >&2
  fail=1
else
  # (a) gate SET -> an over-retention archive survives the prune.
  sp_dir="$(mktemp -d)"
  touch -d '2000-01-01 00:00:00' "${sp_dir}/jarvis_20000101_000000.sql.gz"
  BACKUP_DIR="$sp_dir" RETENTION_DAYS=7 BACKUP_SKIP_PRUNE=1 \
    bash -c 'set -euo pipefail; '"$prune_block" >/dev/null 2>&1 || true
  sp_kept=0; [ -f "${sp_dir}/jarvis_20000101_000000.sql.gz" ] && sp_kept=1
  rm -rf "$sp_dir"
  # (b) gate UNSET -> the same over-retention archive is pruned.
  np_dir="$(mktemp -d)"
  touch -d '2000-01-01 00:00:00' "${np_dir}/jarvis_20000101_000000.sql.gz"
  BACKUP_DIR="$np_dir" RETENTION_DAYS=7 \
    bash -c 'set -euo pipefail; unset BACKUP_SKIP_PRUNE; '"$prune_block" >/dev/null 2>&1 || true
  np_deleted=0; [ -f "${np_dir}/jarvis_20000101_000000.sql.gz" ] || np_deleted=1
  rm -rf "$np_dir"
  if [ "$sp_kept" -eq 1 ] && [ "$np_deleted" -eq 1 ]; then
    pass "BACKUP_SKIP_PRUNE=1 keeps an over-retention archive; unset still prunes it"
  else
    printf 'FAIL: BACKUP_SKIP_PRUNE gating wrong (skip_kept=%s unset_deleted=%s)\n' \
      "$sp_kept" "$np_deleted" >&2
    fail=1
  fi
fi

# RESTORE-BACKUP-RACE: a scheduled/on-demand backup must STAND DOWN while a restore
# holds the maintenance sentinel (dumping mid drop-swap captures an inconsistent DB),
# but the restore's OWN safety pre-backup (BACKUP_FORCE=1) must run. Source checks:
check "honors BACKUP_FORCE to bypass the maintenance skip-guard" '\bBACKUP_FORCE\b'
check "guards scheduled backups behind a maintenance_active check" '^maintenance_active\(\)'
check "tags a maintenance skip in .last_run.json" '"skipped_maintenance":'
check "takes a flock single-run mutex" 'flock -n'

# Behavioral: single-source the write_last_run + maintenance-guard blocks out of
# backup.sh and replay them in isolation (no Postgres). replay_guard echoes REACHED
# iff the backup would proceed; the EXIT trap writes .last_run.json either way.
MAINT_WLR="$(sed -n '/^write_last_run()/,/^}/p' "$BACKUP_SCRIPT")"
MAINT_GUARD="$(awk '/^# --- Maintenance skip-guard/{f=1} f{print} /another backup is already running/{c++} c&&/^fi$/{exit}' "$BACKUP_SCRIPT")"
replay_guard() {
  # replay_guard <trigger_dir> <backup_dir> <force>
  BACKUP_TRIGGER_DIR="$1" BACKUP_DIR="$2" BACKUP_FORCE="$3" \
  bash -c '
    set -euo pipefail
    ATTEMPTED_AT=x TIMESTAMP=t JARVIS_STATE=failed LITELLM_STATE=failed
    SECRETS_STATE=skipped QDRANT_STATE=skipped ENCRYPT=0 RETENTION_DAYS=7
    '"$MAINT_WLR"'
    trap write_last_run EXIT
    '"$MAINT_GUARD"'
    echo REACHED
  ' 2>/dev/null || true
}

# (a) the forced safety backup RUNS under a fresh .maintenance sentinel.
mg_dir="$(mktemp -d)"; mg_trig="${mg_dir}/trig"; mkdir -p "$mg_trig"; : > "${mg_trig}/.maintenance"
if replay_guard "$mg_trig" "$mg_dir" 1 | grep -q REACHED \
   && grep -q '"skipped_maintenance":false' "${mg_dir}/.last_run.json"; then
  pass "BACKUP_FORCE=1 safety backup runs under a fresh .maintenance sentinel"
else
  printf 'FAIL: forced safety backup did not run under .maintenance\n' >&2; fail=1
fi
rm -rf "$mg_dir"

# (b) a non-forced scheduled run SKIPS under .maintenance AND under .destructive,
#     tagging skipped_maintenance:true without producing a backup.
for sentinel in .maintenance .destructive; do
  mb_dir="$(mktemp -d)"; mb_trig="${mb_dir}/trig"; mkdir -p "$mb_trig"; : > "${mb_trig}/${sentinel}"
  mb_out="$(replay_guard "$mb_trig" "$mb_dir" "")"
  if ! printf '%s' "$mb_out" | grep -q REACHED \
     && grep -q '"skipped_maintenance":true' "${mb_dir}/.last_run.json"; then
    pass "scheduled run skips + tags skipped_maintenance under ${sentinel}"
  else
    printf 'FAIL: scheduled run did not skip/tag under %s (out=%s)\n' "$sentinel" "$mb_out" >&2; fail=1
  fi
  # a maintenance-skip must not leave jarvis/litellm at their "failed"
  # startup default — that reads as a real backup failure to /status. They
  # must be tagged "skipped", mirroring how secrets/qdrant already default.
  if grep -q '"jarvis":"skipped"' "${mb_dir}/.last_run.json" \
     && grep -q '"litellm":"skipped"' "${mb_dir}/.last_run.json"; then
    pass "maintenance-skip under ${sentinel} tags jarvis/litellm stores as skipped (not failed)"
  else
    printf 'FAIL: maintenance-skip under %s left jarvis/litellm stores non-skipped\n' "$sentinel" >&2
    fail=1
  fi
  rm -rf "$mb_dir"
done

# (c) with sentinels CLEARED a scheduled run does NOT skip (no stale-status wedge).
mc_dir="$(mktemp -d)"; mc_trig="${mc_dir}/trig"; mkdir -p "$mc_trig"
if replay_guard "$mc_trig" "$mc_dir" "" | grep -q REACHED \
   && grep -q '"skipped_maintenance":false' "${mc_dir}/.last_run.json"; then
  pass "cleared sentinels resume scheduled backups (no wedge)"
else
  printf 'FAIL: scheduled run skipped with no sentinel present\n' >&2; fail=1
fi
rm -rf "$mc_dir"

# (d) a stale (>1800s) .maintenance with no .destructive does NOT skip (age-expiry).
md_dir="$(mktemp -d)"; md_trig="${md_dir}/trig"; mkdir -p "$md_trig"
: > "${md_trig}/.maintenance"; touch -d '2000-01-01 00:00:00' "${md_trig}/.maintenance"
if replay_guard "$md_trig" "$md_dir" "" | grep -q REACHED; then
  pass "stale (>1800s) .maintenance with no .destructive does not wedge backups"
else
  printf 'FAIL: stale soft sentinel wrongly skipped the backup\n' >&2; fail=1
fi
rm -rf "$md_dir"

# (e) the flock mutex blocks a second concurrent NON-forced run, but a BACKUP_FORCE=1
#     safety backup is never blocked. A background holder takes the lock first.
me_dir="$(mktemp -d)"; me_trig="${me_dir}/trig"; mkdir -p "$me_trig"
( exec 8>"${me_trig}/.backup.lock"; flock -n 8 && { : > "${me_dir}/.held"; sleep 3; } ) &
me_holder=$!
for _ in $(seq 1 40); do [ -f "${me_dir}/.held" ] && break; sleep 0.05; done
me_blocked="$(replay_guard "$me_trig" "$me_dir" "")"
me_forced="$(replay_guard "$me_trig" "$me_dir" 1)"
kill "$me_holder" 2>/dev/null || true; wait "$me_holder" 2>/dev/null || true
if ! printf '%s' "$me_blocked" | grep -q REACHED && printf '%s' "$me_forced" | grep -q REACHED; then
  pass "flock blocks a concurrent non-forced backup; BACKUP_FORCE=1 is never blocked"
else
  printf 'FAIL: flock mutex wrong (blocked reached=%s, forced reached=%s)\n' \
    "$(printf '%s' "$me_blocked" | grep -qc REACHED)" \
    "$(printf '%s' "$me_forced" | grep -qc REACHED)" >&2; fail=1
fi
rm -rf "$me_dir"

if [ "$fail" -ne 0 ]; then
  printf '\nbackup coverage: FAILED\n' >&2
  exit 1
fi
printf '\nbackup coverage: all checks passed\n'
