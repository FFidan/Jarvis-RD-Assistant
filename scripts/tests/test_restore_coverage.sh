#!/usr/bin/env bash
# test_restore_coverage.sh — assert scripts/restore.sh implements the hardened
# one-click restore correctly. restore.sh is the highest-risk DR script: its
# failure paths must be fail-safe, so the checks below pin the load-bearing
# invariants (at-most-once consume before destruction, revoke-before-drop,
# never-re-expose-a-destroyed-DB, exit 0 after a recorded failure) both by
# static structure AND by running the pure helpers behaviorally.
#
# Run: bash scripts/tests/test_restore_coverage.sh   (exit 0 = pass)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESTORE_SCRIPT="${SCRIPT_DIR}/../restore.sh"
COMPOSE="${SCRIPT_DIR}/../../docker-compose.yml"

fail=0
pass() { printf 'PASS: %s\n' "$1"; }
check() {
  # check <human description> <grep -E pattern>
  if grep -Eq "$2" "$RESTORE_SCRIPT"; then
    pass "$1"
  else
    printf 'FAIL: %s (pattern: %s)\n' "$1" "$2" >&2
    fail=1
  fi
}
line_of() { grep -nE "$1" "$RESTORE_SCRIPT" | head -1 | cut -d: -f1; }

if [ ! -r "$RESTORE_SCRIPT" ]; then
  printf 'FAIL: cannot read %s\n' "$RESTORE_SCRIPT" >&2
  exit 1
fi

# === Static structure ========================================================

# 1. It is a bash script with strict mode.
check "uses the bash shebang" '^#!/usr/bin/env bash'
check "sets strict mode (set -euo pipefail)" '^set -euo pipefail'

# The sidecar entrypoint execs /usr/local/bin/restore.sh DIRECTLY (no `bash`
# prefix), and the bind mount preserves the host file's mode, so a non-executable
# restore.sh dies with exit 126 and the restore never runs.
if [ -x "$RESTORE_SCRIPT" ]; then
  pass "restore.sh is executable (the sidecar execs it directly)"
else
  printf 'FAIL: restore.sh is NOT executable — the sidecar exec would fail with exit 126\n' >&2
  fail=1
fi

# 2. AT-MOST-ONCE: the request sentinel is consumed (rm -f) BEFORE any
#    destruction — proven by execution order: the step-1 consume (main flow)
#    precedes the step-5 restore_one_db call (main flow).
check "consumes the .restore_request.json sentinel" '\.restore_request\.json'
rm_line="$(line_of 'REQUEST_FILE.*\|\| true')"
drop_call_line="$(line_of 'restore_one_db "\$JARVIS_DB"')"
if [ -n "$rm_line" ] && [ -n "$drop_call_line" ] && [ "$rm_line" -lt "$drop_call_line" ]; then
  pass "request sentinel is rm -f'd BEFORE the destructive restore_one_db call"
else
  printf 'FAIL: request consume (%s) is not before the restore_one_db call (%s)\n' \
    "$rm_line" "$drop_call_line" >&2
  fail=1
fi

# 3. QUIESCE-BY-REVOKE: every DROP DATABASE is preceded by ALTER ... ALLOW_CONNECTIONS
#    false + pg_terminate_backend (revoke CONNECT so pools cannot re-grab the DB).
check "revokes connections before dropping (ALLOW_CONNECTIONS false)" \
  'ALTER DATABASE .* ALLOW_CONNECTIONS false'
check "terminates existing backends before dropping" 'pg_terminate_backend'
# Anchor on the executable psql_admin calls (not the explanatory comments, which
# also name DROP DATABASE / ALLOW_CONNECTIONS).
alter_line="$(line_of 'psql_admin "ALTER DATABASE')"
drop_db_line="$(line_of 'psql_admin "DROP DATABASE')"
if [ -n "$alter_line" ] && [ -n "$drop_db_line" ] && [ "$alter_line" -lt "$drop_db_line" ]; then
  pass "ALTER ... ALLOW_CONNECTIONS false precedes the first DROP DATABASE"
else
  printf 'FAIL: ALLOW_CONNECTIONS false (%s) does not precede DROP DATABASE (%s)\n' \
    "$alter_line" "$drop_db_line" >&2
  fail=1
fi

# 4. Safety pre-backup runs BEFORE the first destructive DROP.
safety_line="$(line_of '^if /usr/local/bin/backup.sh')"
if [ -n "$safety_line" ] && [ -n "$drop_call_line" ] && [ "$safety_line" -lt "$drop_call_line" ]; then
  pass "safety pre-backup (backup.sh) runs before the destructive restore"
else
  printf 'FAIL: safety backup (%s) does not run before the destructive restore (%s)\n' \
    "$safety_line" "$drop_call_line" >&2
  fail=1
fi

# 4b. DATA-RESTORE-PRUNE-RACE: the resolved DB archives are re-verified to still
#     exist AFTER the safety pre-backup and BEFORE the first DROP, so a vanished
#     archive fails before destruction instead of dropping the DB then failing to
#     reload it. The safety backup itself must export BACKUP_SKIP_PRUNE so it
#     cannot prune the restore target.
check "safety pre-backup is run with BACKUP_SKIP_PRUNE so it cannot prune the target" \
  'export BACKUP_SKIP_PRUNE=1'
reverify_line="$(line_of 'disappeared before the restore began')"
if [ -n "$safety_line" ] && [ -n "$reverify_line" ] && [ -n "$drop_call_line" ] \
   && [ "$safety_line" -lt "$reverify_line" ] && [ "$reverify_line" -lt "$drop_call_line" ]; then
  pass "archive re-verify runs between the safety pre-backup and the first DROP"
else
  printf 'FAIL: archive re-verify (%s) is not between safety backup (%s) and the destructive restore (%s)\n' \
    "$reverify_line" "$safety_line" "$drop_call_line" >&2
  fail=1
fi

# 5. DBs are reloaded via decrypt|gunzip|psql — NOT pg_restore (backups are plain SQL).
check "reloads dumps through gunzip" 'gunzip'
check "reloads dumps through psql" 'psql -h "\$PGHOST"'
# pg_restore must not be USED (ignore comments that merely say "NOT pg_restore").
if grep -vE '^[[:space:]]*#' "$RESTORE_SCRIPT" | grep -q 'pg_restore'; then
  printf 'FAIL: restore.sh uses pg_restore (backups are plain SQL — must use gunzip|psql)\n' >&2
  fail=1
else
  pass "does NOT use pg_restore (plain-SQL reload only)"
fi

# 5b. The destructive window is marked at the connection-revoke: DROP_STARTED=1 is
#     set AFTER the successful ALTER and BEFORE the DROP, so a terminate/DROP
#     failure holds maintenance (never lifts the 503 over a non-servable DB).
dropstart_line="$(line_of 'DROP_STARTED=1')"
if [ -n "$alter_line" ] && [ -n "$dropstart_line" ] && [ -n "$drop_db_line" ] \
   && [ "$alter_line" -lt "$dropstart_line" ] && [ "$dropstart_line" -lt "$drop_db_line" ]; then
  pass "DROP_STARTED is marked between the ALTER and the DROP (destructive-window boundary)"
else
  printf 'FAIL: DROP_STARTED (%s) is not between ALTER (%s) and DROP (%s)\n' \
    "$dropstart_line" "$alter_line" "$drop_db_line" >&2
  fail=1
fi

# 5c. Compat gate bounds the backup against the CODE's max migration (stable even
#     when the live DB is gone mid-recovery), NOT a live schema_migrations query
#     (which would refuse every backup during the safety-backup recovery).
check "compat gate reads the code's max migration" 'CODE_MAX='
check "compat gate uses the migrations dir (not the live DB)" 'db/migrations'
if grep -vE '^[[:space:]]*#' "$RESTORE_SCRIPT" | grep -q 'schema_migrations'; then
  printf 'FAIL: compat gate still queries the live schema_migrations (blocks recovery when the DB is gone)\n' >&2
  fail=1
else
  pass "compat gate does NOT query the live schema_migrations table"
fi

# 5d. A wrong/rotated encryption key (or corrupt archive) is caught by a gzip-magic
#     probe BEFORE any DROP — a bad key found mid-reload would leave the DB
#     dropped+empty.
probe_line="$(line_of 'magic=.*decrypt_or_passthrough')"
check "verifies each DB archive's gzip magic before destruction" '1f8b'
if [ -n "$probe_line" ] && [ -n "$drop_call_line" ] && [ "$probe_line" -lt "$drop_call_line" ]; then
  pass "the decrypt/gzip-magic probe runs before the destructive restore"
else
  printf 'FAIL: decrypt probe (%s) does not precede the destructive restore (%s)\n' \
    "$probe_line" "$drop_call_line" >&2
  fail=1
fi

# 6. NEVER-RE-EXPOSE: the EXIT trap lifts .maintenance on a clean restore OR a
#    failure BEFORE the first DROP (DROP_STARTED=0, nothing destroyed); a
#    post-DROP failure (DROP_STARTED=1, not clean) MUST keep the stack 503.
check "lift gate clears maintenance on a clean restore" '\[ "\$RESTORE_CLEAN" = "1" \]'
check "lift gate also clears on a pre-DROP failure (nothing destroyed)" '\|\| \[ "\$DROP_STARTED" = "0" \]'
maint_rm_line="$(line_of 'rm -f "\$MAINTENANCE_SENTINEL"')"
guard_line="$(grep -nE '\[ "\$RESTORE_CLEAN" = "1" \]' "$RESTORE_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$maint_rm_line" ] && [ -n "$guard_line" ] \
   && [ "$guard_line" -lt "$maint_rm_line" ] \
   && [ "$((maint_rm_line - guard_line))" -le 2 ]; then
  pass "the only maintenance-sentinel removal is guarded by the clean flag"
else
  printf 'FAIL: maintenance rm (%s) is not immediately guarded by RESTORE_CLEAN (%s)\n' \
    "$maint_rm_line" "$guard_line" >&2
  fail=1
fi

# 6b. durable .destructive sentinel: never-heartbeated, touched at the
#     DROP-window boundary (after DROP_STARTED=1, before the DROP) and removed ONLY
#     inside the clean lift block, so a SIGKILLed post-DROP restore stays 503 with no
#     age gate (the heartbeat never re-touches it, so it survives the heartbeat dying).
destr_touch_line="$(line_of 'touch "\$MAINTENANCE_DESTRUCTIVE"')"
if [ -n "$dropstart_line" ] && [ -n "$destr_touch_line" ] && [ -n "$drop_db_line" ] \
   && [ "$dropstart_line" -le "$destr_touch_line" ] && [ "$destr_touch_line" -lt "$drop_db_line" ]; then
  pass "the .destructive sentinel is touched at the DROP window (after DROP_STARTED, before the DROP)"
else
  printf 'FAIL: destructive touch (%s) is not between DROP_STARTED (%s) and DROP (%s)\n' \
    "$destr_touch_line" "$dropstart_line" "$drop_db_line" >&2
  fail=1
fi
destr_rm_line="$(line_of 'rm -f "\$MAINTENANCE_DESTRUCTIVE"')"
if [ -n "$destr_rm_line" ] && [ -n "$guard_line" ] \
   && [ "$guard_line" -lt "$destr_rm_line" ] \
   && [ "$((destr_rm_line - guard_line))" -le 3 ]; then
  pass "the .destructive removal is inside the clean lift block (guarded, never unconditional)"
else
  printf 'FAIL: destructive rm (%s) is not within ~3 lines after the lift guard (%s)\n' \
    "$destr_rm_line" "$guard_line" >&2
  fail=1
fi
# The heartbeat re-touch loop must NOT touch .destructive: it has no age gate by
# design, so it must survive a SIGKILL that kills the heartbeat.
if grep -E 'sleep 60; touch' "$RESTORE_SCRIPT" | grep -q 'MAINTENANCE_DESTRUCTIVE'; then
  printf 'FAIL: the heartbeat loop re-touches .destructive (it must never be heartbeated)\n' >&2
  fail=1
else
  pass "the heartbeat loop does not re-touch the .destructive sentinel"
fi

# 6c. PRESENT-but-NO-CHECKSUMS manifest rejected BEFORE the DROP
#     (fail_before_destruction, nothing destroyed); the ABSENT-manifest WARN+proceed
#     back-compat path is unchanged (a present-valid manifest lacking only
#     schema_version still proceeds — NOT rejected).
check "rejects a present-but-corrupt manifest (no archive checksums)" \
  'present but corrupt or incomplete \(no archive checksums\)'
corrupt_line="$(line_of 'present but corrupt or incomplete \(no archive checksums\)')"
if [ -n "$corrupt_line" ] && [ -n "$drop_call_line" ] && [ "$corrupt_line" -lt "$drop_call_line" ]; then
  pass "the present-but-corrupt manifest reject runs before the destructive restore"
else
  printf 'FAIL: corrupt-manifest reject (%s) does not precede the destructive restore (%s)\n' \
    "$corrupt_line" "$drop_call_line" >&2
  fail=1
fi
check "keeps the absent-manifest WARN+proceed back-compat path" 'manifest .* absent; proceeding'

# 6d. older backup restore holds maintenance (it needs forward
#     migrations only an app-container recreate can run), so the lift gate excludes
#     RESTORE_OLDER and STEP 9 prints the operator runbook line.
check "flags an older-than-code backup (RESTORE_OLDER)" 'RESTORE_OLDER=1'
check "lift gate excludes a clean older restore" '\[ "\$RESTORE_OLDER" != "1" \]'
check "STEP 9 prints the older-restore operator runbook line" 'OLDER backup restored'

# 7. The maintenance sentinel is heartbeated for the whole run (re-touch loop) so
#    a long restore does not auto-expire mid-flight.
check "heartbeats the maintenance sentinel during the run" \
  'sleep 60; touch "\$MAINTENANCE_SENTINEL"'

# 8. exit 0 after a recorded terminal failure: every non-zero exit in the file is
#    a perl statement (semicolon-terminated) inside qdrant_http_body — there is NO
#    bash-level non-zero exit that could crash-restart the sidecar.
check "fails before destruction with exit 0" 'fail_before_destruction\(\)'
check "fails during/after the drop with exit 0" 'step5_fail\(\)'
nonzero_all="$(grep -Ec 'exit[[:space:]]+[1-9]' "$RESTORE_SCRIPT" || true)"
nonzero_perl="$(grep -Ec 'exit[[:space:]]+[1-9];' "$RESTORE_SCRIPT" || true)"
if [ "$nonzero_all" -eq "$nonzero_perl" ]; then
  pass "no bash-level non-zero exit (terminal failures exit 0; sidecar never crash-restarts)"
else
  printf 'FAIL: a bash-level non-zero exit exists (all=%s perl=%s)\n' "$nonzero_all" "$nonzero_perl" >&2
  fail=1
fi

# 9. Qdrant recover sends a real JSON body + Content-Type (a body-less PUT 4xxs).
check "qdrant recover sets a request body (content)" '\$opts\{content\}'
check "qdrant recover sets the JSON Content-Type header" 'Content-Type.*application/json'
check "qdrant recover targets the snapshots/recover endpoint with file://" \
  'snapshots/recover'
check "qdrant recover uses a file:// location under the staging dir" \
  'file://\$\{QDRANT_STAGING_DIR\}'

# 10. bash syntax is valid.
if bash -n "$RESTORE_SCRIPT"; then
  pass "bash -n parses restore.sh"
else
  printf 'FAIL: bash -n found a syntax error in restore.sh\n' >&2
  fail=1
fi

# === Behavioral (pure helpers; no real DB / Qdrant) ==========================

# B1. decrypt_or_passthrough: passthrough (no .enc) round-trips stdin verbatim,
#     AND the .enc branch openssl-decrypts back to the original bytes (catching a
#     silent no-op decrypt). Single-sourced from restore.sh's own openssl recipe.
dp_dir="$(mktemp -d)"
dp_key="${dp_dir}/key.txt"
printf 'restore-test-passphrase' > "$dp_key"
printf 'HELLO-STDIN' > "${dp_dir}/plain"
pass_out="$(
  ENC_KEYFILE="$dp_key" bash -c '
    set -euo pipefail
    '"$(sed -n '/^decrypt_or_passthrough()/,/^}/p' "$RESTORE_SCRIPT")"'
    printf "HELLO-STDIN" | decrypt_or_passthrough
  ' 2>/dev/null
)"
dec_recipe="$(grep -oE 'openssl enc -d -aes-256-cbc -pbkdf2 -iter [0-9]+' "$RESTORE_SCRIPT" | head -1)"
enc_recipe="${dec_recipe/ -d/}"
$enc_recipe -kfile "$dp_key" < "${dp_dir}/plain" > "${dp_dir}/fixture.enc" 2>/dev/null
dec_out="$(
  ENC_KEYFILE="$dp_key" bash -c '
    set -euo pipefail
    '"$(sed -n '/^decrypt_or_passthrough()/,/^}/p' "$RESTORE_SCRIPT")"'
    decrypt_or_passthrough "'"${dp_dir}/fixture.enc"'"
  ' 2>/dev/null
)"
if [ "$pass_out" = "HELLO-STDIN" ] && [ "$dec_out" = "HELLO-STDIN" ]; then
  pass "decrypt_or_passthrough: passthrough round-trips stdin AND .enc decrypts to original"
else
  printf 'FAIL: decrypt_or_passthrough wrong (passthrough=%s decrypt=%s)\n' "$pass_out" "$dec_out" >&2
  fail=1
fi
rm -rf "$dp_dir"

# B2. valid_archive_name accepts the four shapes, rejects path-seps / .. / junk.
vfn="$(sed -n '/^valid_archive_name()/,/^}/p' "$RESTORE_SCRIPT")"
run_valid() {
  bash -c '
    set -euo pipefail
    '"$vfn"'
    if valid_archive_name "$1"; then echo OK; else echo NO; fi
  ' _ "$1" 2>/dev/null
}
vfail=0
for good in \
  "jarvis_20260626_120000.sql.gz" \
  "litellm_20260626_120000.sql.gz.enc" \
  "secrets_20260626_120000.tar.gz" \
  "qdrant_kg_entities_20260626_120000.snapshot.enc"; do
  [ "$(run_valid "$good")" = "OK" ] || { printf 'FAIL: valid name rejected: %s\n' "$good" >&2; vfail=1; }
done
for bad in \
  "../etc/passwd" \
  "jarvis_20260626_120000.sql.gz/x" \
  "qdrant_../_20260626_120000.snapshot" \
  "manifest_20260626_120000.json" \
  "evil.sql.gz" \
  "jarvis_2026_120000.sql.gz"; do
  [ "$(run_valid "$bad")" = "NO" ] || { printf 'FAIL: invalid name accepted: %s\n' "$bad" >&2; vfail=1; }
done
if [ "$vfail" -eq 0 ]; then
  pass "valid_archive_name accepts the 4 shapes, rejects path-seps/../junk"
else
  fail=1
fi

# B3. write_status emits the P6.3 RestoreStatus shape as valid JSON (5 named
#     steps, escaped error string, all required keys present).
if command -v python3 >/dev/null 2>&1; then
  st_dir="$(mktemp -d)"
  st_out="${st_dir}/status.json"
  STATUS_FILE="$st_out" bash -c '
    set -euo pipefail
    STATE="running"; CURRENT_STEP="Restoring database"; ERROR="boom \"q\" \\ x"
    SAFETY_BACKUP_TS="20260626_120000"; STARTED_AT="2026-06-26T12:00:00+00:00"
    FINISHED_AT=""; DROP_STARTED=1
    STEP_SAFETY="done"; STEP_DB="running"; STEP_LITELLM="pending"
    STEP_QDRANT="pending"; STEP_FINISH="pending"
    '"$(sed -n '/^_json_escape()/,/^}/p' "$RESTORE_SCRIPT")"'
    '"$(sed -n '/^_json_or_null()/,/^}/p' "$RESTORE_SCRIPT")"'
    '"$(sed -n '/^write_status()/,/^}/p' "$RESTORE_SCRIPT")"'
    write_status
  ' 2>/dev/null
  if python3 - "$st_out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["state"] == "running", d
names = [s["name"] for s in d["steps"]]
assert names == ["Safety backup", "Restoring database", "Restoring API-key store",
                 "Restoring search index", "Finishing up"], names
for k in ("state", "current_step", "steps", "safety_backup_ts",
          "started_at", "finished_at", "error"):
    assert k in d, k
assert d["safety_backup_ts"] == "20260626_120000"
assert d["error"] == 'boom "q" \\ x', repr(d["error"])
PY
  then
    pass "write_status emits valid JSON matching the RestoreStatus shape"
  else
    printf 'FAIL: write_status did not emit a valid RestoreStatus JSON\n' >&2
    fail=1
  fi
  rm -rf "$st_dir"
else
  printf 'SKIP: python3 unavailable; skipping write_status JSON validation\n' >&2
fi

# === Off-host (inbox) disaster recovery ======================================
# P6.8: an ADDITIVE source="inbox" branch restores from the rw restore_inbox
# volume on a fresh host — operator-supplied archive set + one-time key, secrets
# materialized to a writable staging (never /secrets), postgres role rebound via
# ALTER ROLE. Every pre-ALTER-ROLE failure must destroy nothing; the operator key
# + plaintext staging are shredded on every exit. The local path stays unchanged.
BACKUP_SCRIPT="${SCRIPT_DIR}/../backup.sh"

# I1. The request source is parsed and defaults to local; an unsupported value
#     fails safe (it is NOT silently treated as local).
check "parses the request source field" '"source"'
check "defaults the restore source to local when absent" 'SOURCE="\$\{SOURCE_RAW:-local\}"'
check "rejects an unsupported source (fail-safe, not silent-local)" 'expected local or inbox'

# I2. A separate ARCHIVE_DIR drives the archive lookup for inbox; BACKUP_DIR (the
#     STEP-4 safety pre-backup + .last_run.json target) is NOT clobbered.
check "introduces a separate ARCHIVE_DIR for inbox" 'ARCHIVE_DIR="\$INBOX_DIR"'
check "globs the archive set from ARCHIVE_DIR (not a hardcoded BACKUP_DIR)" \
  'for f in "\$\{ARCHIVE_DIR\}"'
check "keeps BACKUP_DIR for the safety pre-backup .last_run.json read" \
  'BACKUP_DIR\}/\.last_run\.json'

# I3. The inbox branch points decryption at the operator key and validates it
#     BEFORE any destruction (a missing key fails safe; the STEP-2 decrypt probe
#     then proves a wrong key fails before any DROP).
check "sets ENC_KEYFILE to the inbox operator key for inbox restores" \
  'ENC_KEYFILE="\$OPERATOR_KEYFILE"'
opkey_line="$(line_of '\[ ! -s "\$OPERATOR_KEYFILE" \]')"
if [ -n "$opkey_line" ] && [ -n "$drop_call_line" ] && [ "$opkey_line" -lt "$drop_call_line" ]; then
  pass "operator-key presence is validated before the destructive restore"
else
  printf 'FAIL: operator-key check (%s) does not precede the destructive restore (%s)\n' \
    "$opkey_line" "$drop_call_line" >&2
  fail=1
fi

# I4. STEP 8 rebinds the postgres role AFTER the DB restore, doubling the
#     password's single quotes for the SQL string literal.
alter_role_line="$(line_of 'ALTER ROLE .*WITH PASSWORD')"
if [ -n "$alter_role_line" ] && [ -n "$drop_call_line" ] && [ "$drop_call_line" -lt "$alter_role_line" ]; then
  pass "the ALTER ROLE rebind runs AFTER the destructive DB restore"
else
  printf 'FAIL: ALTER ROLE (%s) does not run after the destructive restore (%s)\n' \
    "$alter_role_line" "$drop_call_line" >&2
  fail=1
fi
check "doubles single quotes in the rebind password literal (no SQL injection)" \
  'OLD_PG_PW//'

# I5. All inbox additions are guarded by source=inbox (the local path is unchanged).
check "guards the inbox secrets/role step behind source=inbox" \
  'if \[ "\$SOURCE" = "inbox" \]'

# I6. The operator key is shredded + the plaintext staging removed on EVERY exit.
check "shreds the one-time operator key on cleanup" 'shred -u "\$OPERATOR_KEYFILE"'
check "removes the plaintext secrets staging on cleanup" 'rm -rf "\$SECRETS_STAGING"'

# I7. Secrets staging lives under the inbox, NEVER under the RO /secrets mount.
check "stages secrets under the inbox volume" 'SECRETS_STAGING="\$\{INBOX_DIR\}'
if grep -E 'SECRETS_STAGING=' "$RESTORE_SCRIPT" | grep -q '/secrets'; then
  printf 'FAIL: SECRETS_STAGING is under /secrets (must be the rw inbox, never the RO /secrets)\n' >&2
  fail=1
else
  pass "secrets staging is never under the RO /secrets"
fi

# I9. A clean INBOX restore does NOT lift maintenance (the app holds the new-host
#     password until the operator recreates it); only same-host clean lifts.
check "holds maintenance on a clean inbox restore (lift gate excludes inbox)" \
  '\[ "\$SOURCE" != "inbox" \]'

# I9b. The clean inbox path enters the destructive window, so it holds the durable
#      .destructive sentinel (never auto-expires). The STEP-9 inbox echo MUST name
#      it, or an operator clearing only .maintenance bricks the recovered stack at
#      HTTP 503 (mirrors the OLDER echo, 6d).
check "the inbox STEP-9 echo tells the operator to clear .destructive too" \
  'off-host restore complete.*/backup-trigger/\.destructive'

# I10. An empty restored postgres_password fails safe instead of setting a blank
#      role password (the [ ! -s ] file-size guard alone would pass a newline-only file).
check "rejects an empty restored postgres password before ALTER ROLE" \
  'if \[ -z "\$OLD_PG_PW" \]'

# I11. The decrypted plaintext secret bundle is shredded (not just rm'd) on exit.
check "shreds the staged plaintext secret files (not a bare rm)" \
  'find "\$SECRETS_STAGING" -type f -exec shred -u'

# I12. Cleanup fires whenever the operator key / staging exist — not only when
#      SOURCE resolved to inbox — so a malformed source field still shreds the key.
check "shreds the operator key even if the source field was malformed" \
  '\[ -e "\$OPERATOR_KEYFILE" \]'

# I8. backup.sh has an aws-guarded S3 pull helper that the default scheduled
#     backup never triggers (gated on BACKUP_PULL_TS).
bcheck() {
  if grep -Eq "$2" "$BACKUP_SCRIPT"; then pass "$1"; else
    printf 'FAIL: %s (pattern: %s)\n' "$1" "$2" >&2; fail=1; fi
}
bcheck "backup.sh defines an S3 pull helper" '^pull_from_s3\(\)'
bcheck "the S3 pull is gated on BACKUP_PULL_TS (default backup unchanged)" 'BACKUP_PULL_TS'
bcheck "the S3 pull downloads the timestamp's archive set from the bucket" \
  'aws s3 cp "s3://\$\{BACKUP_S3_BUCKET\}/"'

# === Compose wiring ==========================================================

cmp_check() {
  if grep -Eq "$2" "$COMPOSE"; then pass "$1"; else
    printf 'FAIL: %s (pattern: %s)\n' "$1" "$2" >&2; fail=1; fi
}
cmp_check "sidecar mounts restore.sh" 'restore\.sh:/usr/local/bin/restore\.sh:ro'
cmp_check "sidecar env stamps JARVIS_VERSION (manifest app_version)" 'JARVIS_VERSION: \$\{JARVIS_VERSION'
cmp_check "entrypoint runs restore.sh on a restore request" \
  'restore_request\.json.*restore\.sh|if \[ -f /backup-trigger/\.restore_request\.json'
cmp_check "named volume restore_staging is declared" '^  restore_staging:'
cmp_check "sidecar mounts the migrations dir (ro) for the compat code-max read" 'db/migrations:/app/db/migrations:ro'

# Off-host DR drop zone: a rw restore_inbox volume the operator fills with the
# archive set + one-time key for a cross-host (inbox) restore.
cmp_check "named volume restore_inbox is declared" '^  restore_inbox:'
cmp_check "sidecar mounts restore_inbox at /restore-inbox" 'restore_inbox:/restore-inbox'
if grep -qE 'restore_inbox:/restore-inbox:ro' "$COMPOSE"; then
  printf 'FAIL: restore_inbox is mounted :ro (must be rw — the operator writes the archive set + key here)\n' >&2
  fail=1
else
  pass "restore_inbox is mounted rw (not :ro)"
fi

# restore_staging mounted into BOTH the sidecar (rw) and qdrant (ro).
if [ "$(grep -c 'restore_staging:/qdrant/snapshots/restore' "$COMPOSE")" -ge 2 ]; then
  pass "restore_staging mounted into both the sidecar and qdrant"
else
  printf 'FAIL: restore_staging not mounted into both sidecar and qdrant\n' >&2
  fail=1
fi

# backup_trigger mounted into learning_engine too (so the maintenance middleware
# can stat the sentinel there) — now in paper_ingestion + sidecar + learning_engine.
if [ "$(grep -c 'backup_trigger:/backup-trigger' "$COMPOSE")" -ge 3 ]; then
  pass "backup_trigger mounted into learning_engine (>=3 services)"
else
  printf 'FAIL: backup_trigger not mounted into learning_engine\n' >&2
  fail=1
fi

# Full compose validation when docker is available; otherwise a YAML lint.
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if ( cd "${SCRIPT_DIR}/../.." && docker compose config -q ) 2>/dev/null; then
    pass "docker compose config validates"
  else
    printf 'FAIL: docker compose config -q rejected the compose file\n' >&2
    fail=1
  fi
elif command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' "$COMPOSE" 2>/dev/null; then
    pass "docker-compose.yml is well-formed YAML (docker unavailable; lint only)"
  else
    printf 'FAIL: docker-compose.yml is not well-formed YAML\n' >&2
    fail=1
  fi
else
  printf 'SKIP: neither docker nor python3 available for compose validation\n' >&2
fi

if [ "$fail" -ne 0 ]; then
  printf '\nrestore coverage: FAILED\n' >&2
  exit 1
fi
printf '\nrestore coverage: all checks passed\n'
