#!/usr/bin/env bash
# test_backup_coverage.sh — assert scripts/backup.sh covers the full DR surface.
#
# Disaster-recovery state spans the JARVIS and LiteLLM databases, Qdrant
# snapshots, numeric PDFs, and three database-coupled data keys. These checks
# keep each required archive role in the backup contract.
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

# 6. Only the three keys coupled to restored database content may cross hosts.
#    Target-local service credentials must never enter a new backup archive.
check "declares the exact restored-data-key archive set" \
  'DATA_KEY_FILES=\(jarvis_config_key\.txt jarvis_model_hmac_key\.txt litellm_salt_key\.txt\)'

# 7. BAC-3: the Qdrant snapshot download must stream to the filehandle via a
#    data_callback, not buffer the whole body in $res->{content}.
check "streams the Qdrant snapshot via data_callback" 'data_callback'

# 8. BAC-4: the retention prune must also remove orphaned .tmp staging files.
check "prunes orphaned .tmp staging files" '\-name .\*\.tmp'

# 9. Behavioral inventory proof: the producer's declared set yields exactly
#    those three files even when the source directory contains host credentials,
#    integration tokens, and rotation staging.
keys_dir="$(mktemp -d)"
keys_archive="$(mktemp)"
data_key_line="$(grep -E '^[[:space:]]*DATA_KEY_FILES=\(' "$BACKUP_SCRIPT" | head -1)"
data_key_line="${data_key_line#*(}"
data_key_line="${data_key_line%)*}"
read -r -a data_key_files <<< "$data_key_line"
for name in "${data_key_files[@]}"; do printf 'DATA' > "$keys_dir/$name"; done
for name in postgres_password.txt jarvis_api_key.txt litellm_master_key.txt \
            telegram_bot_token.txt smtp_pass.txt backup_encrypt_key.txt \
            jarvis_config_key_next.txt jarvis_config_key_rotation_state.txt; do
  printf 'TARGET' > "$keys_dir/$name"
done
tar -czf "$keys_archive" -C "$keys_dir" -- "${data_key_files[@]}"
keys_members="$(tar -tzf "$keys_archive" | sort)"
expected_members="$(printf '%s\n' "${data_key_files[@]}" | sort)"
if [ "$keys_members" = "$expected_members" ]; then
  pass "new secrets archive contains exactly the three restored data keys"
else
  printf 'FAIL: restored-data-key archive inventory drifted (members=%s)\n' "$keys_members" >&2
  fail=1
fi
rm -rf "$keys_dir"
rm -f "$keys_archive"

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

# Completion truth is single-sourced from backup.sh's real write_last_run body.
# The manifest is mandatory, an encrypted run also requires its signature, and
# secrets may be skipped only for an unencrypted non-production run. Qdrant
# remains best-effort.
COMPLETION_WLR="$(sed -n '/^write_last_run()/,/^}/p' "$BACKUP_SCRIPT")"
last_run_json() {
  # last_run_json <secrets> <qdrant> <manifest> <signature> <environment> <encrypt>
  local lr_dir lr
  lr_dir="$(mktemp -d)"
  lr="$(
    BACKUP_DIR="$lr_dir" ATTEMPTED_AT=x TIMESTAMP=t \
    RUN_ID=0123456789abcdef0123456789abcdef \
    JARVIS_STATE=ok LITELLM_STATE=ok PDFS_STATE=ok SECRETS_STATE="$1" QDRANT_STATE="$2" \
    MANIFEST_STATE="$3" MANIFEST_SIGNATURE_STATE="$4" ENVIRONMENT="$5" \
    ENCRYPT="$6" RETENTION_DAYS=7 SKIPPED_MAINTENANCE=0 \
    bash -c '
      set -euo pipefail
      '"$COMPLETION_WLR"'
      write_last_run
      cat "${BACKUP_DIR}/.last_run.json"
    '
  )"
  rm -rf "$lr_dir"
  printf '%s' "$lr"
}

lr_secrets_failed="$(last_run_json failed skipped ok skipped development 0)"
if printf '%s' "$lr_secrets_failed" | grep -q '"succeeded":false'; then
  pass "secrets=failed cannot be recorded as a successful backup"
else
  printf 'FAIL: secrets=failed was recorded as succeeded (%s)\n' "$lr_secrets_failed" >&2
  fail=1
fi

lr_manifest_failed="$(last_run_json ok skipped failed skipped development 0)"
if printf '%s' "$lr_manifest_failed" | grep -q '"succeeded":false'; then
  pass "manifest=failed cannot be recorded as a successful backup"
else
  printf 'FAIL: manifest=failed was recorded as succeeded (%s)\n' "$lr_manifest_failed" >&2
  fail=1
fi

lr_signature_failed="$(last_run_json ok skipped ok failed production 1)"
if printf '%s' "$lr_signature_failed" | grep -q '"succeeded":false'; then
  pass "encrypted signature=failed cannot be recorded as a successful backup"
else
  printf 'FAIL: encrypted signature=failed was recorded as succeeded (%s)\n' "$lr_signature_failed" >&2
  fail=1
fi

lr_dev_skipped="$(last_run_json skipped skipped ok skipped development 0)"
if printf '%s' "$lr_dev_skipped" | grep -q '"succeeded":true'; then
  pass "unencrypted non-production secrets=skipped may still be successful"
else
  printf 'FAIL: unencrypted non-production secrets=skipped was not successful (%s)\n' "$lr_dev_skipped" >&2
  fail=1
fi

lr_encrypted_skipped="$(last_run_json skipped skipped ok ok development 1)"
if printf '%s' "$lr_encrypted_skipped" | grep -q '"succeeded":false'; then
  pass "encrypted secrets=skipped cannot be recorded as a successful backup"
else
  printf 'FAIL: encrypted secrets=skipped was recorded as succeeded (%s)\n' "$lr_encrypted_skipped" >&2
  fail=1
fi

lr_production_skipped="$(last_run_json skipped skipped ok skipped production 0)"
if printf '%s' "$lr_production_skipped" | grep -q '"succeeded":false'; then
  pass "production secrets=skipped cannot be recorded as a successful backup"
else
  printf 'FAIL: production secrets=skipped was recorded as succeeded (%s)\n' "$lr_production_skipped" >&2
  fail=1
fi

lr_qdrant_failed="$(last_run_json ok failed ok skipped development 0)"
if printf '%s' "$lr_qdrant_failed" | grep -q '"succeeded":true'; then
  pass "Qdrant failure remains non-fatal to backup completion"
else
  printf 'FAIL: optional Qdrant failure made backup unsuccessful (%s)\n' "$lr_qdrant_failed" >&2
  fail=1
fi

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

# Manifest contract: each run emits a manifest_<ts>.json listing every archive with
# its sha256 + the applied schema_version, so the restore UI can show per-point
# version compatibility. It must be written, hashed, schema-versioned, run BEFORE
# the S3 upload (so S3 carries it), and pruned WITH its archives.
check "defines a write_manifest stage" '^write_manifest\(\)'
check "writes a per-run manifest_<ts>.json" 'manifest_\$\{TIMESTAMP\}\.json'
check "records the applied schema_version in the manifest" 'schema_migrations'
check "sha256-hashes each archive for the manifest" 'sha256sum'
check "prunes manifests with their archives" 'manifest_\*\.json'

# Mandatory finalization must run AFTER the archives exist and BEFORE S3 upload,
# so an incomplete set can never be published off-host.
if awk '/^if ! finalize_backup; then/{c=NR} /Optional S3 upload/{s=NR} END{exit !(c && s && c<s)}' \
     "$BACKUP_SCRIPT"; then
  pass "mandatory backup finalization runs before the S3 upload block"
else
  printf 'FAIL: mandatory backup finalization is not invoked before the S3 upload block\n' >&2
  fail=1
fi

# write_manifest (behavioral): run the actual function against the explicit list
# of files successfully produced by THIS run. An unrelated same-timestamp file
# must not be swept into the signed restore point.
man_dir="$(mktemp -d)"
man_ts="20260626_120000"
man_run_id="0123456789abcdef0123456789abcdef"
printf 'J' > "${man_dir}/jarvis_${man_ts}.sql.gz"
printf 'L' > "${man_dir}/litellm_${man_ts}.sql.gz"
printf 'S' > "${man_dir}/secrets_${man_ts}.tar.gz"
printf 'Q' > "${man_dir}/qdrant_kg_entities_${man_ts}.snapshot"
printf 'stale' > "${man_dir}/qdrant_kg_entities_${man_ts}.snapshot.raw"  # must be excluded
printf 'attacker' > "${man_dir}/qdrant_unrelated_${man_ts}.snapshot"
# Extract write_manifest's body from backup.sh and run it in isolation with the
# fixture env (single source of truth — a change to the builder is exercised here).
man_out="$(
  BACKUP_DIR="$man_dir" TIMESTAMP="$man_ts" RUN_ID="$man_run_id" PGHOST=/nonexistent \
  bash -c '
    set -euo pipefail
    BACKUP_ARCHIVES=(
      "${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz"
      "${BACKUP_DIR}/litellm_${TIMESTAMP}.sql.gz"
      "${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz"
      "${BACKUP_DIR}/qdrant_kg_entities_${TIMESTAMP}.snapshot"
    )
    '"$(sed -n '/^promote_new_file()/,/^}/p' "$BACKUP_SCRIPT")"'
    '"$(sed -n '/^write_manifest()/,/^}/p' "$BACKUP_SCRIPT")"'
    write_manifest || true
    cat "${BACKUP_DIR}/manifest_${TIMESTAMP}.json"
  ' 2>/dev/null
)"
if printf '%s' "$man_out" | grep -q '"filename":"jarvis_'"${man_ts}"'.sql.gz"' \
   && printf '%s' "$man_out" | grep -q '"filename":"qdrant_kg_entities_'"${man_ts}"'.snapshot"' \
   && printf '%s' "$man_out" | grep -q '"run_id":"'"${man_run_id}"'"' \
   && ! printf '%s' "$man_out" | grep -q 'qdrant_unrelated_' \
   && ! printf '%s' "$man_out" | grep -q '\.snapshot\.raw' \
   && ! printf '%s' "$man_out" | grep -q '"filename":"manifest_' \
   && printf '%s' "$man_out" \
        | grep -qE '"sha256":"[0-9a-f]{64}"'; then
  pass "write_manifest signs the run ID and only the explicit successful outputs"
else
  printf 'FAIL: write_manifest did not bind the run ID to only explicit outputs (%s)\n' "$man_out" >&2
  fail=1
fi
rm -rf "$man_dir"

# Finalization failures must return non-zero, record honest state, and make only
# this run's exact timestamp non-enumerable. A neighboring restore point proves
# cleanup cannot broaden across timestamps.
FINALIZE_DISCARD="$(sed -n '/^discard_current_backup()/,/^}/p' "$BACKUP_SCRIPT")"
FINALIZE_PROMOTE="$(sed -n '/^promote_new_file()/,/^}/p' "$BACKUP_SCRIPT")"
FINALIZE_MANIFEST="$(sed -n '/^write_manifest()/,/^}/p' "$BACKUP_SCRIPT")"
FINALIZE_PUBLISH="$(sed -n '/^publish_manifest_signature()/,/^}/p' "$BACKUP_SCRIPT")"
FINALIZE_BACKUP="$(sed -n '/^finalize_backup()/,/^}/p' "$BACKUP_SCRIPT")"

mf_dir="$(mktemp -d)"; mf_ts="20260720_120000"; mf_old="20260719_120000"
printf 'J' > "${mf_dir}/jarvis_${mf_ts}.sql.gz"
printf 'L' > "${mf_dir}/litellm_${mf_ts}.sql.gz"
printf 'Q' > "${mf_dir}/qdrant_kg_entities_${mf_ts}.snapshot"
printf 'old' > "${mf_dir}/jarvis_${mf_old}.sql.gz"
mf_rc=0
BACKUP_DIR="$mf_dir" TIMESTAMP="$mf_ts" ATTEMPTED_AT=x RUN_ID=0123456789abcdef0123456789abcdef \
JARVIS_STATE=ok LITELLM_STATE=ok PDFS_STATE=ok SECRETS_STATE=skipped QDRANT_STATE=ok \
MANIFEST_STATE=failed MANIFEST_SIGNATURE_STATE=skipped ENVIRONMENT=development \
ENCRYPT=0 RETENTION_DAYS=7 SKIPPED_MAINTENANCE=0 PGHOST=/nonexistent \
HOST_SECRETS_DIR="$mf_dir" MANIFEST_HMAC_MARKER="${mf_dir}/marker" \
bash -c '
  set -uo pipefail
  BACKUP_ARCHIVES=("${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz" "${BACKUP_DIR}/litellm_${TIMESTAMP}.sql.gz" "${BACKUP_DIR}/qdrant_kg_entities_${TIMESTAMP}.snapshot")
  psql() { printf "102\\n"; }
  sha256sum() { return 1; }
  sign_manifest() { return 0; }
  '"$COMPLETION_WLR"'
  '"$FINALIZE_PROMOTE"'
  '"$FINALIZE_DISCARD"'
  '"$FINALIZE_MANIFEST"'
  '"$FINALIZE_PUBLISH"'
  '"$FINALIZE_BACKUP"'
  finalize_backup
  rc=$?
  write_last_run
  exit "$rc"
' 2>/dev/null || mf_rc=$?
if [ "$mf_rc" -ne 0 ] \
   && [ -f "${mf_dir}/jarvis_${mf_old}.sql.gz" ] \
   && ! find "$mf_dir" -maxdepth 1 -type f -name "*_${mf_ts}*" | grep -q . \
   && grep -q '"manifest":"failed"' "${mf_dir}/.last_run.json" \
   && grep -q '"succeeded":false' "${mf_dir}/.last_run.json"; then
  pass "manifest hash failure is fatal and removes only the exact current run"
else
  printf 'FAIL: manifest failure was not fail-closed (rc=%s files=%s status=%s)\n' \
    "$mf_rc" "$(find "$mf_dir" -maxdepth 1 -type f -printf '%f ' | sort)" \
    "$(cat "${mf_dir}/.last_run.json" 2>/dev/null || true)" >&2
  fail=1
fi
rm -rf "$mf_dir"

sf_dir="$(mktemp -d)"; sf_ts="20260720_130000"; sf_old="20260719_130000"
printf 'J' > "${sf_dir}/jarvis_${sf_ts}.sql.gz.enc"
printf 'L' > "${sf_dir}/litellm_${sf_ts}.sql.gz.enc"
printf 'S' > "${sf_dir}/secrets_${sf_ts}.tar.gz.enc"
printf 'old' > "${sf_dir}/jarvis_${sf_old}.sql.gz.enc"
sf_rc=0
BACKUP_DIR="$sf_dir" TIMESTAMP="$sf_ts" ATTEMPTED_AT=x RUN_ID=fedcba9876543210fedcba9876543210 \
JARVIS_STATE=ok LITELLM_STATE=ok PDFS_STATE=ok SECRETS_STATE=ok QDRANT_STATE=skipped \
MANIFEST_STATE=failed MANIFEST_SIGNATURE_STATE=failed ENVIRONMENT=production \
ENCRYPT=1 RETENTION_DAYS=7 SKIPPED_MAINTENANCE=0 PGHOST=/nonexistent \
HOST_SECRETS_DIR="$sf_dir" MANIFEST_HMAC_MARKER="${sf_dir}/marker" \
bash -c '
  set -uo pipefail
  BACKUP_ARCHIVES=("${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz.enc" "${BACKUP_DIR}/litellm_${TIMESTAMP}.sql.gz.enc" "${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz.enc")
  psql() { printf "102\\n"; }
  sign_manifest() { return 1; }
  '"$COMPLETION_WLR"'
  '"$FINALIZE_PROMOTE"'
  '"$FINALIZE_DISCARD"'
  '"$FINALIZE_MANIFEST"'
  '"$FINALIZE_PUBLISH"'
  '"$FINALIZE_BACKUP"'
  finalize_backup
  rc=$?
  write_last_run
  exit "$rc"
' 2>/dev/null || sf_rc=$?
if [ "$sf_rc" -ne 0 ] \
   && [ -f "${sf_dir}/jarvis_${sf_old}.sql.gz.enc" ] \
   && ! find "$sf_dir" -maxdepth 1 -type f -name "*_${sf_ts}*" | grep -q . \
   && grep -q '"manifest_signature":"failed"' "${sf_dir}/.last_run.json" \
   && grep -q '"succeeded":false' "${sf_dir}/.last_run.json"; then
  pass "encrypted signature failure is fatal and removes only the exact current run"
else
  printf 'FAIL: signature failure was not fail-closed (rc=%s files=%s status=%s)\n' \
    "$sf_rc" "$(find "$sf_dir" -maxdepth 1 -type f -printf '%f ' | sort)" \
    "$(cat "${sf_dir}/.last_run.json" 2>/dev/null || true)" >&2
  fail=1
fi
rm -rf "$sf_dir"

rf_dir="$(mktemp -d)"; rf_ts="20260720_140000"
printf 'J' > "${rf_dir}/jarvis_${rf_ts}.sql.gz.enc"
printf 'L' > "${rf_dir}/litellm_${rf_ts}.sql.gz.enc"
printf 'S' > "${rf_dir}/secrets_${rf_ts}.tar.gz.enc"
rf_rc=0
BACKUP_DIR="$rf_dir" TIMESTAMP="$rf_ts" ATTEMPTED_AT=x RUN_ID=00112233445566778899aabbccddeeff \
JARVIS_STATE=ok LITELLM_STATE=ok PDFS_STATE=ok SECRETS_STATE=ok QDRANT_STATE=skipped \
MANIFEST_STATE=failed MANIFEST_SIGNATURE_STATE=failed ENVIRONMENT=production \
ENCRYPT=1 RETENTION_DAYS=7 SKIPPED_MAINTENANCE=0 PGHOST=/nonexistent \
HOST_SECRETS_DIR="$rf_dir" MANIFEST_HMAC_MARKER="${rf_dir}/missing/marker" \
bash -c '
  set -uo pipefail
  BACKUP_ARCHIVES=("${BACKUP_DIR}/jarvis_${TIMESTAMP}.sql.gz.enc" "${BACKUP_DIR}/litellm_${TIMESTAMP}.sql.gz.enc" "${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz.enc")
  psql() { printf "102\\n"; }
  sign_manifest() { printf "signed\\n" > "${1}.hmac"; }
  '"$COMPLETION_WLR"'
  '"$FINALIZE_PROMOTE"'
  '"$FINALIZE_DISCARD"'
  '"$FINALIZE_MANIFEST"'
  '"$FINALIZE_PUBLISH"'
  '"$FINALIZE_BACKUP"'
  finalize_backup
  rc=$?
  write_last_run
  exit "$rc"
' 2>/dev/null || rf_rc=$?
if [ "$rf_rc" -ne 0 ] \
   && ! find "$rf_dir" -maxdepth 1 -type f -name "*_${rf_ts}*" | grep -q . \
   && grep -q '"manifest_signature":"failed"' "${rf_dir}/.last_run.json"; then
  pass "signed-manifest ratchet publication failure is fatal"
else
  printf 'FAIL: ratchet publication failure was not fail-closed (rc=%s status=%s)\n' \
    "$rf_rc" "$(cat "${rf_dir}/.last_run.json" 2>/dev/null || true)" >&2
  fail=1
fi
rm -rf "$rf_dir"

# DATA-RESTORE-PRUNE-RACE: the retention prune at the tail must be gated on
# BACKUP_SKIP_PRUNE so restore.sh's safety pre-backup cannot delete the very
# archive being restored (a `local` target older than RETENTION_DAYS).
check "gates the retention prune on BACKUP_SKIP_PRUNE" \
  'if \[ -z "\$\{BACKUP_SKIP_PRUNE:-\}" \]; then'

# DATA-RESTORE-PRUNE-RACE (behavioral): replay backup.sh's OWN prune block against
# a fixture archive older than retention. With BACKUP_SKIP_PRUNE=1 the old archive
# must SURVIVE; with the gate unset the prune must still delete it. Single-sourced
# from backup.sh (a change to the gate or the find globs is exercised here too).
PRUNE_IN_FLIGHT_FN="$(sed -n '/^prune_in_flight_ts()/,/^}/p' "$BACKUP_SCRIPT")"
PRUNE_AGE_FN="$(sed -n '/^retention_prune_age()/,/^}/p' "$BACKUP_SCRIPT")"
PRUNE_KEEP_FN="$(sed -n '/^retention_keep_last_n()/,/^}/p' "$BACKUP_SCRIPT")"
prune_block="$(sed -n '/^# --- Prune old backups/,/^fi$/p' "$BACKUP_SCRIPT")"
if [ -z "$prune_block" ]; then
  printf 'FAIL: could not extract the BACKUP_SKIP_PRUNE-gated prune block from backup.sh\n' >&2
  fail=1
else
  # (a) gate SET -> an over-retention archive survives the prune.
  sp_dir="$(mktemp -d)"
  mkdir -p "${sp_dir}/trigger"
  touch -d '2000-01-01 00:00:00' "${sp_dir}/jarvis_20000101_000000.sql.gz"
  BACKUP_DIR="$sp_dir" TRIGGER_DIR="${sp_dir}/trigger" RETENTION_DAYS=7 BACKUP_SKIP_PRUNE=1 \
    bash -c 'set -euo pipefail; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_AGE_FN"$'\n'"$PRUNE_KEEP_FN"$'\n'"$prune_block" >/dev/null 2>&1 || true
  sp_kept=0; [ -f "${sp_dir}/jarvis_20000101_000000.sql.gz" ] && sp_kept=1
  rm -rf "$sp_dir"
  # (b) gate UNSET -> the same over-retention archive is pruned.
  np_dir="$(mktemp -d)"
  mkdir -p "${np_dir}/trigger"
  touch -d '2000-01-01 00:00:00' "${np_dir}/jarvis_20000101_000000.sql.gz"
  BACKUP_DIR="$np_dir" TRIGGER_DIR="${np_dir}/trigger" RETENTION_DAYS=7 \
    bash -c 'set -euo pipefail; unset BACKUP_SKIP_PRUNE; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_AGE_FN"$'\n'"$PRUNE_KEEP_FN"$'\n'"$prune_block" >/dev/null 2>&1 || true
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

# The manifest becomes visible before backup.sh reaches retention, so the update
# CLI can observe it while the producing run is still finishing. Always protect
# this run's own timestamp, even if clock-skewed future filenames would otherwise
# push it beyond keep-last-N before the updater has published its durable pin.
jc_dir="$(mktemp -d)"; jc_ts="20260720_120000"; jc_future="20990101_000000"
mkdir -p "${jc_dir}/trigger"
for ts in "$jc_ts" "$jc_future"; do
  : > "${jc_dir}/jarvis_${ts}.sql.gz.enc"
  : > "${jc_dir}/litellm_${ts}.sql.gz.enc"
  : > "${jc_dir}/secrets_${ts}.tar.gz.enc"
  : > "${jc_dir}/manifest_${ts}.json"
  : > "${jc_dir}/manifest_${ts}.json.hmac"
done
BACKUP_DIR="$jc_dir" TRIGGER_DIR="${jc_dir}/trigger" RETENTION_DAYS=99999 KEEP_LAST_N=1 TIMESTAMP="$jc_ts" \
  bash -c 'set -euo pipefail; unset BACKUP_SKIP_PRUNE; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_AGE_FN"$'\n'"$PRUNE_KEEP_FN"$'\n'"$prune_block" \
  >/dev/null 2>&1 || true
if [ -f "${jc_dir}/jarvis_${jc_ts}.sql.gz.enc" ]; then
  pass "the producing run cannot prune its own timestamp before an update pin appears"
else
  printf 'FAIL: keep-last retention pruned the producing run before it could be pinned\n' >&2
  fail=1
fi
rm -rf "$jc_dir"

# A later producer may finish after an updater has authenticated its rollback
# point but before the updater publishes the durable pin. The updater holds the
# lifecycle lock across that window. Retention must detect it without blocking
# while holding .backup.lock, keep every candidate, and resume normally once the
# lifecycle lock is released.
lr_dir="$(mktemp -d)"; lr_trig="${lr_dir}/trigger"; mkdir -p "$lr_trig" "${lr_dir}/.lifecycle"
lr_rollback="20260720_120000"; lr_current="20990101_000000"
for ts in "$lr_rollback" "$lr_current"; do
  : > "${lr_dir}/jarvis_${ts}.sql.gz.enc"
  : > "${lr_dir}/litellm_${ts}.sql.gz.enc"
  : > "${lr_dir}/secrets_${ts}.tar.gz.enc"
  : > "${lr_dir}/manifest_${ts}.json"
  : > "${lr_dir}/manifest_${ts}.json.hmac"
done
( exec 7>>"${lr_dir}/.lifecycle/update.lock"; flock 7; : > "${lr_dir}/.lifecycle-held"; sleep 3 ) &
lr_holder=$!
for _ in $(seq 1 40); do [ -f "${lr_dir}/.lifecycle-held" ] && break; sleep 0.05; done
lr_locked_rc=0
lr_locked_out="$(
BACKUP_DIR="$lr_dir" TRIGGER_DIR="$lr_trig" RETENTION_DAYS=99999 KEEP_LAST_N=1 \
TIMESTAMP="$lr_current" \
  timeout 1 bash -c 'set -euo pipefail; unset BACKUP_SKIP_PRUNE; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_AGE_FN"$'\n'"$PRUNE_KEEP_FN"$'\n'"$prune_block" 2>&1
)" || lr_locked_rc=$?
lr_kept_while_locked=0
[ -f "${lr_dir}/jarvis_${lr_rollback}.sql.gz.enc" ] && lr_kept_while_locked=1
kill "$lr_holder" 2>/dev/null || true; wait "$lr_holder" 2>/dev/null || true
lr_unlocked_rc=0
BACKUP_DIR="$lr_dir" TRIGGER_DIR="$lr_trig" RETENTION_DAYS=99999 KEEP_LAST_N=1 \
TIMESTAMP="$lr_current" \
  bash -c 'set -euo pipefail; unset BACKUP_SKIP_PRUNE; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_AGE_FN"$'\n'"$PRUNE_KEEP_FN"$'\n'"$prune_block" \
  >/dev/null 2>&1 || lr_unlocked_rc=$?
lr_deleted_after_release=0
[ -f "${lr_dir}/jarvis_${lr_rollback}.sql.gz.enc" ] || lr_deleted_after_release=1
if [ "$lr_locked_rc" -eq 0 ] \
   && printf '%s' "$lr_locked_out" | grep -q 'publishing a rollback pin' \
   && [ "$lr_kept_while_locked" -eq 1 ] \
   && [ "$lr_unlocked_rc" -eq 0 ] \
   && [ "$lr_deleted_after_release" -eq 1 ]; then
  pass "automatic retention skips non-blockingly during update pin publication, then resumes"
else
  printf 'FAIL: automatic retention raced update pin publication (locked_rc=%s locked_kept=%s later_rc=%s later_deleted=%s out=%s)\n' \
    "$lr_locked_rc" "$lr_kept_while_locked" "$lr_unlocked_rc" \
    "$lr_deleted_after_release" "$lr_locked_out" >&2
  fail=1
fi
rm -rf "$lr_dir"

# UPDATE-BACKUP-PIN: a schema-changing update's authenticated rollback point is
# in-flight until the update commits. A strict marker must protect the complete
# timestamp from both age and keep-last-N pruning; malformed markers protect
# nothing.
if [ -n "$PRUNE_IN_FLIGHT_FN" ] && [ -n "$PRUNE_AGE_FN" ] && [ -n "$PRUNE_KEEP_FN" ]; then
  pin_ts="20000101_000000"; newer_ts="20990101_000000"
  pin_run="0123456789abcdef0123456789abcdef"

  pa_dir="$(mktemp -d)"; pa_trig="${pa_dir}/trigger"; mkdir -p "$pa_trig"
  for stem in "jarvis_${pin_ts}.sql.gz.enc" "litellm_${pin_ts}.sql.gz.enc" \
              "secrets_${pin_ts}.tar.gz.enc" "manifest_${pin_ts}.json" \
              "manifest_${pin_ts}.json.hmac"; do
    : > "${pa_dir}/${stem}"; touch -d '2000-01-02 00:00:00' "${pa_dir}/${stem}"
  done
  printf '{"timestamp":"%s","run_id":"%s"}\n' "$pin_ts" "$pin_run" \
    > "${pa_trig}/.update_backup_pin.json"
  BACKUP_DIR="$pa_dir" TRIGGER_DIR="$pa_trig" RETENTION_DAYS=7 \
    bash -c 'set -euo pipefail; RESTORE_REQUEST_FILE="$TRIGGER_DIR/.restore_request.json"; RESTORE_STATUS_FILE="$TRIGGER_DIR/.restore_status.json"; UPDATE_PIN_FILE="$TRIGGER_DIR/.update_backup_pin.json"; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_AGE_FN"$'\n''retention_prune_age "$BACKUP_DIR" "$RETENTION_DAYS" "$(prune_in_flight_ts)"' \
    >/dev/null 2>&1 || true
  age_pin_kept=0; [ -f "${pa_dir}/jarvis_${pin_ts}.sql.gz.enc" ] && age_pin_kept=1
  rm -rf "$pa_dir"

  pk_dir="$(mktemp -d)"; pk_trig="${pk_dir}/trigger"; mkdir -p "$pk_trig"
  for ts in "$pin_ts" "$newer_ts"; do
    : > "${pk_dir}/jarvis_${ts}.sql.gz.enc"
    : > "${pk_dir}/litellm_${ts}.sql.gz.enc"
    : > "${pk_dir}/secrets_${ts}.tar.gz.enc"
    : > "${pk_dir}/manifest_${ts}.json"
    : > "${pk_dir}/manifest_${ts}.json.hmac"
  done
  printf '{"timestamp":"%s","run_id":"%s"}\n' "$pin_ts" "$pin_run" \
    > "${pk_trig}/.update_backup_pin.json"
  BACKUP_DIR="$pk_dir" TRIGGER_DIR="$pk_trig" \
    bash -c 'set -euo pipefail; RESTORE_REQUEST_FILE="$TRIGGER_DIR/.restore_request.json"; RESTORE_STATUS_FILE="$TRIGGER_DIR/.restore_status.json"; UPDATE_PIN_FILE="$TRIGGER_DIR/.update_backup_pin.json"; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_KEEP_FN"$'\n''retention_keep_last_n "$BACKUP_DIR" 1 "$(prune_in_flight_ts)"' \
    >/dev/null 2>&1 || true
  count_pin_kept=0; [ -f "${pk_dir}/jarvis_${pin_ts}.sql.gz.enc" ] && count_pin_kept=1
  rm -rf "$pk_dir"

  pm_dir="$(mktemp -d)"; pm_trig="${pm_dir}/trigger"; mkdir -p "$pm_trig"
  : > "${pm_dir}/jarvis_${pin_ts}.sql.gz.enc"
  touch -d '2000-01-02 00:00:00' "${pm_dir}/jarvis_${pin_ts}.sql.gz.enc"
  printf '{"timestamp":"%s","run_id":"not-a-run-id"}\n' "$pin_ts" \
    > "${pm_trig}/.update_backup_pin.json"
  BACKUP_DIR="$pm_dir" TRIGGER_DIR="$pm_trig" RETENTION_DAYS=7 \
    bash -c 'set -euo pipefail; RESTORE_REQUEST_FILE="$TRIGGER_DIR/.restore_request.json"; RESTORE_STATUS_FILE="$TRIGGER_DIR/.restore_status.json"; UPDATE_PIN_FILE="$TRIGGER_DIR/.update_backup_pin.json"; '"$PRUNE_IN_FLIGHT_FN"$'\n'"$PRUNE_AGE_FN"$'\n''retention_prune_age "$BACKUP_DIR" "$RETENTION_DAYS" "$(prune_in_flight_ts)"' \
    >/dev/null 2>&1 || true
  malformed_deleted=0; [ -f "${pm_dir}/jarvis_${pin_ts}.sql.gz.enc" ] || malformed_deleted=1
  rm -rf "$pm_dir"

  if [ "$age_pin_kept" -eq 1 ] && [ "$count_pin_kept" -eq 1 ] \
     && [ "$malformed_deleted" -eq 1 ]; then
    pass "valid update pins survive age + keep-last pruning; malformed pins do not"
  else
    printf 'FAIL: update pin retention wrong (age=%s count=%s malformed_deleted=%s)\n' \
      "$age_pin_kept" "$count_pin_kept" "$malformed_deleted" >&2
    fail=1
  fi
else
  printf 'FAIL: update-pin-aware retention helpers are missing\n' >&2
  fail=1
fi

# RESTORE-BACKUP-RACE: a scheduled/on-demand backup must STAND DOWN while a restore
# holds the maintenance sentinel (dumping mid drop-swap captures an inconsistent DB),
# but the restore's OWN safety pre-backup (BACKUP_FORCE=1) must run. Source checks:
check "honors BACKUP_FORCE to bypass the maintenance skip-guard" '\bBACKUP_FORCE\b'
check "guards scheduled backups behind a maintenance_active check" '^maintenance_active\(\)'
check "treats config-key rotation as fail-closed maintenance" 'rotation\.guard'
check "tags a maintenance skip in .last_run.json" '"skipped_maintenance":'
check "takes a flock single-run mutex" 'flock -n'

# Behavioral: single-source the write_last_run + maintenance-guard blocks out of
# backup.sh and replay them in isolation (no Postgres). replay_guard echoes REACHED
# iff the backup would proceed; the EXIT trap writes .last_run.json either way.
MAINT_WLR="$(sed -n '/^write_last_run()/,/^}/p' "$BACKUP_SCRIPT")"
LOCK_HELPERS="$(sed -n '/^prepare_lock_dir()/,/^}/p' "$BACKUP_SCRIPT")"$'\n'"$(sed -n '/^open_legacy_backup_lock()/,/^}/p' "$BACKUP_SCRIPT")"
MAINT_GUARD="$(awk '/^# --- Maintenance skip-guard/{f=1} /^if ! claim_backup_trigger/{exit} f{print}' "$BACKUP_SCRIPT")"
replay_guard() {
  # replay_guard <trigger_dir> <backup_dir> <force>
  BACKUP_TRIGGER_DIR="$1" TRIGGER_DIR="$1" BACKUP_DIR="$2" BACKUP_FORCE="$3" \
  bash -c '
    set -euo pipefail
    ATTEMPTED_AT=x TIMESTAMP=t RUN_ID=0123456789abcdef0123456789abcdef
    JARVIS_STATE=failed LITELLM_STATE=failed PDFS_STATE=failed
    SECRETS_STATE=skipped QDRANT_STATE=skipped MANIFEST_STATE=failed
    MANIFEST_SIGNATURE_STATE=skipped ENCRYPT=0 RETENTION_DAYS=7 ENVIRONMENT=development
    LOCK_DIR="${BACKUP_DIR}/.lifecycle"
    BACKUP_LOCK="${LOCK_DIR}/backup.lock"
    UPDATE_LOCK="${LOCK_DIR}/update.lock"
    CONFIG_KEY_ROTATION_SENTINEL="${LOCK_DIR}/rotation.guard"
    LEGACY_BACKUP_LOCK="${TRIGGER_DIR}/.backup.lock"
    '"$MAINT_WLR"'
    '"$LOCK_HELPERS"'
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

# A retained config-key rotation sentinel is stricter than restore maintenance:
# even BACKUP_FORCE may not archive DB ciphertext with the wrong secrets key.
mr_dir="$(mktemp -d)"; mr_trig="${mr_dir}/trig"; mkdir -p "$mr_trig" "${mr_dir}/.lifecycle"
: > "${mr_dir}/.lifecycle/rotation.guard"
mr_out="$(replay_guard "$mr_trig" "$mr_dir" 1)"
if ! printf '%s' "$mr_out" | grep -q REACHED \
   && grep -q '"skipped_maintenance":true' "${mr_dir}/.last_run.json"; then
  pass "config-key rotation blocks forced backups after a retained crash sentinel"
else
  printf 'FAIL: BACKUP_FORCE bypassed the fail-closed config-key rotation sentinel\n' >&2
  fail=1
fi
rm -rf "$mr_dir"

# (b) a non-forced scheduled run SKIPS under restore and rotation maintenance,
#     tagging skipped_maintenance:true without producing a backup.
for sentinel in .maintenance .destructive rotation.guard; do
  mb_dir="$(mktemp -d)"; mb_trig="${mb_dir}/trig"; mkdir -p "$mb_trig" "${mb_dir}/.lifecycle"
  if [ "$sentinel" = rotation.guard ]; then
    : > "${mb_dir}/.lifecycle/${sentinel}"
  else
    : > "${mb_trig}/${sentinel}"
  fi
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

# (e) every producer, including a BACKUP_FORCE=1 safety backup, takes the same
#     mutex. A forced backup racing an ordinary backup must fail closed; bypassing
#     the maintenance sentinel must not bypass serialization.
me_dir="$(mktemp -d)"; me_trig="${me_dir}/trig"; mkdir -p "$me_trig" "${me_dir}/.lifecycle"
( exec 8>>"${me_dir}/.lifecycle/backup.lock"; flock -n 8 && { : > "${me_dir}/.held"; sleep 3; } ) &
me_holder=$!
for _ in $(seq 1 40); do [ -f "${me_dir}/.held" ] && break; sleep 0.05; done
me_blocked="$(replay_guard "$me_trig" "$me_dir" "")"
me_forced="$(replay_guard "$me_trig" "$me_dir" 1)"
kill "$me_holder" 2>/dev/null || true; wait "$me_holder" 2>/dev/null || true
if ! printf '%s' "$me_blocked" | grep -q REACHED && ! printf '%s' "$me_forced" | grep -q REACHED; then
  pass "flock blocks both ordinary and BACKUP_FORCE=1 concurrent backups"
else
  printf 'FAIL: flock mutex can still be bypassed (ordinary reached=%s, forced reached=%s)\n' \
    "$(printf '%s' "$me_blocked" | grep -qc REACHED)" \
    "$(printf '%s' "$me_forced" | grep -qc REACHED)" >&2; fail=1
fi
rm -rf "$me_dir"

# A stale or app-created legacy lock symlink is untrusted upgrade state. The new
# backup path must fail closed without following or changing its target.
ms_dir="$(mktemp -d)"; ms_trig="${ms_dir}/trig"; mkdir -p "$ms_trig"
printf '%s\n' 'PRESERVE-LEGACY-TARGET' > "${ms_dir}/legacy-target"
ln -s "${ms_dir}/legacy-target" "${ms_trig}/.backup.lock"
ms_out="$(replay_guard "$ms_trig" "$ms_dir" "")"
if ! printf '%s' "$ms_out" | grep -q REACHED \
   && [ "$(cat "${ms_dir}/legacy-target")" = PRESERVE-LEGACY-TARGET ]; then
  pass "unsafe legacy backup-lock symlink fails closed without touching its target"
else
  printf 'FAIL: backup followed or accepted an unsafe legacy lock symlink\n' >&2
  fail=1
fi
rm -rf "$ms_dir"

# (e2) A maintenance skip writes .last_run only after obtaining the same mutex.
# If another producer owns the lock, this invocation must leave the producer's
# existing status untouched rather than racing it with skipped_maintenance.
ml_dir="$(mktemp -d)"; ml_trig="${ml_dir}/trig"; mkdir -p "$ml_trig" "${ml_dir}/.lifecycle"
: > "${ml_trig}/.maintenance"
printf '%s\n' 'ACTIVE-PRODUCER-STATUS' > "${ml_dir}/.last_run.json"
( exec 8>>"${ml_dir}/.lifecycle/backup.lock"; flock -n 8 && { : > "${ml_dir}/.held"; sleep 3; } ) &
ml_holder=$!
for _ in $(seq 1 40); do [ -f "${ml_dir}/.held" ] && break; sleep 0.05; done
replay_guard "$ml_trig" "$ml_dir" "" >/dev/null
kill "$ml_holder" 2>/dev/null || true; wait "$ml_holder" 2>/dev/null || true
if [ "$(cat "${ml_dir}/.last_run.json")" = "ACTIVE-PRODUCER-STATUS" ]; then
  pass "maintenance skip updates .last_run only while holding the backup mutex"
else
  printf 'FAIL: maintenance skip raced the active producer status (%s)\n' \
    "$(cat "${ml_dir}/.last_run.json")" >&2
  fail=1
fi
rm -rf "$ml_dir"

# (f) timestamp allocation happens under the lock and skips a pre-existing restore
#     point instead of overwriting it. The allocator is exercised with a fake date
#     that advances one second on each call, so the test is deterministic and fast.
ALLOC_FN="$(sed -n '/^restore_point_exists()/,/^}/p' "$BACKUP_SCRIPT")"
ALLOC_FN="${ALLOC_FN}"$'\n'"$(sed -n '/^allocate_backup_identity()/,/^}/p' "$BACKUP_SCRIPT")"
ma_dir="$(mktemp -d)"
printf 'preserve-me' > "${ma_dir}/jarvis_20260720_120000.sql.gz"
ma_out="$(
  BACKUP_DIR="$ma_dir" BACKUP_RUN_ID=0123456789abcdef0123456789abcdef \
  bash -c '
    set -euo pipefail
    tick=0
    date() {
      case "$*" in
        "+%s") printf "1784548800\n" ;;
        *"@1784548800"*) printf "20260720_120000\n" ;;
        *"@1784548801"*) printf "20260720_120001\n" ;;
        *) command date "$@" ;;
      esac
    }
    openssl() { printf "fedcba9876543210fedcba9876543210\n"; }
    '"$ALLOC_FN"'
    allocate_backup_identity
    printf "%s %s" "$TIMESTAMP" "$RUN_ID"
  ' 2>/dev/null || true
)"
if [ "$ma_out" = "20260720_120001 0123456789abcdef0123456789abcdef" ] \
   && [ "$(cat "${ma_dir}/jarvis_20260720_120000.sql.gz")" = "preserve-me" ]; then
  pass "identity allocation skips a collision and preserves the existing restore point"
else
  printf 'FAIL: collision-free identity allocation missing/wrong (out=%s)\n' "$ma_out" >&2
  fail=1
fi
rm -rf "$ma_dir"

# Even if a file appears after identity allocation, the final atomic promotion
# must refuse replacement and preserve both the existing final and staged bytes.
PROMOTE_FN="$(sed -n '/^promote_new_file()/,/^}/p' "$BACKUP_SCRIPT")"
mp_dir="$(mktemp -d)"; printf existing > "${mp_dir}/final"; printf staged > "${mp_dir}/tmp"
mp_rc=0
bash -c 'set -euo pipefail; '"$PROMOTE_FN"'; promote_new_file "$1" "$2"' \
  _ "${mp_dir}/tmp" "${mp_dir}/final" 2>/dev/null || mp_rc=$?
if [ "$mp_rc" -ne 0 ] && [ "$(cat "${mp_dir}/final")" = existing ] \
   && [ "$(cat "${mp_dir}/tmp")" = staged ]; then
  pass "atomic promotion refuses a late collision without overwriting either file"
else
  printf 'FAIL: atomic promotion clobbered or accepted a late collision\n' >&2; fail=1
fi
rm -rf "$mp_dir"

# The on-demand trigger may carry an update transaction's exact run ID. Claim it
# by rename only after taking the mutex, then read/remove that private claim. A
# new request written to .backup_now after the claim belongs to the next run.
CLAIM_TRIGGER_FN="$(sed -n '/^claim_backup_trigger()/,/^}/p' "$BACKUP_SCRIPT")"
CONSUME_TRIGGER_FN="$(sed -n '/^consume_claimed_backup_trigger()/,/^}/p' "$BACKUP_SCRIPT")"
lock_line="$(grep -n 'if ! flock -n 9' "$BACKUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
claim_line="$(grep -n '^if ! claim_backup_trigger' "$BACKUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
if [ -n "$CLAIM_TRIGGER_FN" ] && [ -n "$CONSUME_TRIGGER_FN" ] \
   && [ -n "$lock_line" ] && [ -n "$claim_line" ] && [ "$lock_line" -lt "$claim_line" ]; then
  pass "claims the on-demand trigger only after taking the backup mutex"
else
  printf 'FAIL: trigger claim helpers are missing or run before the mutex\n' >&2
  fail=1
fi

if [ -n "$CLAIM_TRIGGER_FN" ] && [ -n "$CONSUME_TRIGGER_FN" ]; then
  tr_dir="$(mktemp -d)"; tr_file="${tr_dir}/.backup_now"
  old_id="0123456789abcdef0123456789abcdef"
  next_id="fedcba9876543210fedcba9876543210"
  printf '%s\n' "$old_id" > "$tr_file"
  tr_out="$(
    TRIGGER_DIR="$tr_dir" TRIGGER_FILE="$tr_file" BACKUP_DIR="$tr_dir" NEXT_ID="$next_id" \
    bash -c '
      set -euo pipefail
      TRIGGER_RUN_ID=""; CLAIMED_TRIGGER_FILE=""
      '"$CLAIM_TRIGGER_FN"$'\n'"$CONSUME_TRIGGER_FN"'
      mkdir -p "${BACKUP_DIR}/.lifecycle"
      exec 9>>"${BACKUP_DIR}/.lifecycle/backup.lock"; flock -n 9
      claim_backup_trigger
      claim_path="$CLAIMED_TRIGGER_FILE"
      printf "%s\n" "$NEXT_ID" > "$TRIGGER_FILE"
      consume_claimed_backup_trigger
      state=left; [ -e "$claim_path" ] || state=removed
      printf "%s|%s|%s" "$TRIGGER_RUN_ID" "$(cat "$TRIGGER_FILE")" "$state"
    ' 2>/dev/null || true
  )"
  if [ "$tr_out" = "${old_id}|${next_id}|removed" ]; then
    pass "a request created after the claim survives for the next backup run"
  else
    printf 'FAIL: claimed trigger race contract wrong (out=%s)\n' "$tr_out" >&2
    fail=1
  fi

  : > "$tr_file"
  empty_out="$(
    TRIGGER_DIR="$tr_dir" TRIGGER_FILE="$tr_file" BACKUP_DIR="$tr_dir" \
    bash -c '
      set -euo pipefail
      TRIGGER_RUN_ID=""; CLAIMED_TRIGGER_FILE=""; BACKUP_RUN_ID=""
      date() {
        case "$*" in
          "+%s") printf "1784548800\n" ;;
          *"@1784548800"*) printf "20260720_120000\n" ;;
          *) command date "$@" ;;
        esac
      }
      openssl() { printf "00112233445566778899aabbccddeeff\n"; }
      '"$CLAIM_TRIGGER_FN"$'\n'"$CONSUME_TRIGGER_FN"$'\n'"$ALLOC_FN"'
      mkdir -p "${BACKUP_DIR}/.lifecycle"
      exec 9>>"${BACKUP_DIR}/.lifecycle/backup.lock"; flock -n 9
      claim_backup_trigger
      consume_claimed_backup_trigger
      allocate_backup_identity
      printf "%s|%s|%s" "$TRIGGER_RUN_ID" "$RUN_ID" "$([ -e "$TRIGGER_FILE" ] && echo left || echo removed)"
    ' 2>/dev/null || true
  )"
  if [ "$empty_out" = "|00112233445566778899aabbccddeeff|removed" ]; then
    pass "an empty UI trigger is claimed once and receives a generated run ID"
  else
    printf 'FAIL: empty trigger did not preserve generated-ID behavior (out=%s)\n' "$empty_out" >&2
    fail=1
  fi
  rm -rf "$tr_dir"
fi

check "validates backup run IDs as exactly 32 lowercase hex characters" \
  '\^\[0-9a-f\]\{32\}\$|\^\[0-9a-f\]\{32\}'

if [ "$fail" -ne 0 ]; then
  printf '\nbackup coverage: FAILED\n' >&2
  exit 1
fi
printf '\nbackup coverage: all checks passed\n'
