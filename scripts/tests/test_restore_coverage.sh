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
# The STEP-5 restore_one_db_swap call is the destructive step (it opens the
# disallow->terminate->rename window); anchor all "before any destruction" checks
# on it. (The swap's own reload into a tmp db is non-destructive, so this anchor is
# strictly conservative — every pre-destruction guard genuinely runs before it.)
drop_call_line="$(line_of 'restore_one_db_swap "\$JARVIS_DB"')"
consume_line="$(line_of 'REQ_CONTENT=.*consume_restore_request')"
if [ -n "$consume_line" ] && [ -n "$drop_call_line" ] && [ "$consume_line" -lt "$drop_call_line" ]; then
  pass "request consumption succeeds before the destructive restore_one_db_swap call"
else
  printf 'FAIL: request consumption (%s) is not before the restore_one_db_swap call (%s)\n' \
    "$consume_line" "$drop_call_line" >&2
  fail=1
fi
consume_fn="$(sed -n '/^consume_restore_request()/,/^}/p' "$RESTORE_SCRIPT")"
if printf '%s' "$consume_fn" | grep -q 'rm -f -- "\$REQUEST_FILE"' \
   && ! printf '%s' "$consume_fn" | grep -q 'rm -f -- "\$REQUEST_FILE".*|| true'; then
  pass "request consumption requires the sentinel unlink to succeed"
else
  printf 'FAIL: request consumption can proceed after a failed sentinel unlink\n' >&2
  fail=1
fi

# 3. RENAME-SWAP SEQUENCE (the load-bearing invariant): inside restore_one_db_swap
#    the plain-SQL dump is reloaded into a fresh <db>_restore_tmp and structurally
#    verified BEFORE any destruction; only then does the destructive window open
#    (DROP_STARTED=1 -> disallow -> terminate -> rename-out -> rename-in), gated by a
#    post-swap structural verify that guards the DROP of the <db>_pre_restore
#    rollback snapshot. Proven by strict line-ordering WITHIN the function body.
check "revokes connections before the swap (ALLOW_CONNECTIONS false)" \
  'ALTER DATABASE .* ALLOW_CONNECTIONS false'
check "terminates existing backends before the rename" 'pg_terminate_backend'
swap_fn="$(sed -n '/^restore_one_db_swap()/,/^}/p' "$RESTORE_SCRIPT")"
sf_line() { printf '%s\n' "$swap_fn" | grep -nE "$1" | head -1 | cut -d: -f1; }
# strictly-increasing sequence of the swap's load-bearing steps
swap_seq_ok=1
prev=0; prev_desc="start"
for step in \
  'reload into tmp|psql -h "\$PGHOST".*-d "\$tmp"' \
  'verify tmp|verify_db_structural "\$tmp"' \
  'mark destructive window|DROP_STARTED=1' \
  'disallow connections|ALLOW_CONNECTIONS false' \
  'rename out to pre_restore|RENAME TO .*pre' \
  'rename tmp in|RENAME TO .*db' \
  'post-swap verify (sole gate)|verify_db_structural "\$db"' \
  'drop pre_restore|DROP DATABASE .*pre'; do
  desc="${step%%|*}"; pat="${step#*|}"
  n="$(sf_line "$pat")"
  if [ -z "$n" ] || [ "$n" -le "$prev" ]; then
    printf 'FAIL: swap sequence out of order: "%s" (line %s) not after "%s" (line %s)\n' \
      "$desc" "${n:-none}" "$prev_desc" "$prev" >&2
    swap_seq_ok=0; fail=1; break
  fi
  prev="$n"; prev_desc="$desc"
done
[ "$swap_seq_ok" = "1" ] && pass "restore_one_db_swap: reload+verify-tmp precede the disallow/rename; post-swap verify gates the pre_restore drop"

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

# 5b. The destructive window is marked only AFTER the tmp reload passes structural
#     verify: DROP_STARTED=1 sits between the tmp verify and the disallow-ALTER, so
#     a bad archive / ENOSPC / tmp-verify failure leaves DROP_STARTED=0 (production
#     untouched -> the lift gate clears maintenance).
sf_verify_tmp="$(sf_line 'verify_db_structural "\$tmp"')"
sf_dropstart="$(sf_line 'DROP_STARTED=1')"
sf_disallow="$(sf_line 'ALLOW_CONNECTIONS false')"
if [ -n "$sf_verify_tmp" ] && [ -n "$sf_dropstart" ] && [ -n "$sf_disallow" ] \
   && [ "$sf_verify_tmp" -lt "$sf_dropstart" ] && [ "$sf_dropstart" -lt "$sf_disallow" ]; then
  pass "DROP_STARTED is marked after the tmp verify and before the disallow (non-destructive reload)"
else
  printf 'FAIL: DROP_STARTED (%s) is not between the tmp verify (%s) and the disallow (%s)\n' \
    "$sf_dropstart" "$sf_verify_tmp" "$sf_disallow" >&2
  fail=1
fi

# 5c. Compat gate bounds the backup against the CODE's max migration via a glob (not
#     `ls`, which SC2012-warns), stable even when the live DB is gone mid-recovery,
#     and it performs NO live query (no psql / schema_migrations in STEP 2) — a
#     live-schema query would refuse every backup during the safety-backup recovery.
check "compat gate reads the code's max migration" 'CODE_MAX='
check "compat gate uses the migrations dir (not the live DB)" 'db/migrations'
check "compat gate globs the migrations dir (no ls; SC2012-clean)" 'for _mig in "\$MIG_DIR"'
step2_block="$(sed -n '/=== STEP 2:/,/=== STEP 3:/p' "$RESTORE_SCRIPT")"
if printf '%s' "$step2_block" | grep -qE 'psql|schema_migrations'; then
  printf 'FAIL: compat gate (STEP 2) queries the live DB (blocks recovery when the DB is gone)\n' >&2
  fail=1
else
  pass "compat gate does NOT query the live DB (file-based CODE_MAX only)"
fi

# 5c2. CODE_MAX guardrail: the CODE_MAX fallback literal (used only when both the
#      migrations glob AND the db/SCHEMA_VERSION reads come up empty) must
#      track the code's actual schema floor, or a schema-N backup is wrongly
#      refused as "newer than code" after a migrations squash.
code_max_literal="$(grep -oE 'CODE_MAX=[0-9]+' "$RESTORE_SCRIPT" | grep -oE '[0-9]+' | tail -1)"
schema_version="$(cat "${SCRIPT_DIR}/../../db/SCHEMA_VERSION")"
if [ -n "$code_max_literal" ] && [ "$code_max_literal" -eq "$schema_version" ]; then
  pass "CODE_MAX fallback literal (${code_max_literal}) matches db/SCHEMA_VERSION (${schema_version})"
else
  printf 'FAIL: CODE_MAX fallback literal (%s) does not match db/SCHEMA_VERSION (%s)\n' \
    "$code_max_literal" "$schema_version" >&2
  fail=1
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

# 5d-bis. The probe distinguishes a MISSING key from a wrong/corrupt one: a keyless
#     host meeting an encrypted (.enc) archive is told the key is absent, not sent
#     chasing a rotation or corruption it does not have. The probe block and its
#     decrypt helper are run for real against fixture archives with a stubbed refusal.
decrypt_fn="$(sed -n '/^decrypt_or_passthrough() {/,/^}/p' "$RESTORE_SCRIPT")"
probe_block="$(sed -n '/^for arch in "\$JARVIS_ARCHIVE" "\$LITELLM_ARCHIVE"; do$/,/^done$/p' "$RESTORE_SCRIPT")"
run_probe() {
  # run_probe <archive path> <ENC_KEYFILE>
  bash -c '
    set -euo pipefail
    fail_before_destruction() { printf "REFUSED %s" "$1"; exit 9; }
    JARVIS_ARCHIVE="$1"
    LITELLM_ARCHIVE="$1"
    ENC_KEYFILE="$2"
    '"$decrypt_fn"'
    '"$probe_block"'
    printf "PROCEEDED"
  ' _ "$1" "$2" 2>/dev/null
}
probe_tmp="$(mktemp -d)"
printf 'not a gzip stream' > "${probe_tmp}/jarvis_20260801_120000.sql.gz.enc"
printf 'not a gzip stream' > "${probe_tmp}/jarvis_20260801_120000.sql.gz"
printf 'a-real-looking-key' > "${probe_tmp}/key"
missing_key_refusal="$(run_probe "${probe_tmp}/jarvis_20260801_120000.sql.gz.enc" '' || true)"
wrong_key_refusal="$(run_probe "${probe_tmp}/jarvis_20260801_120000.sql.gz.enc" "${probe_tmp}/key" || true)"
plain_corrupt_refusal="$(run_probe "${probe_tmp}/jarvis_20260801_120000.sql.gz" '' || true)"
rm -rf "$probe_tmp"
if [ -n "$probe_block" ] \
   && printf '%s' "$missing_key_refusal" | grep -q 'no usable backup encryption key' \
   && printf '%s' "$wrong_key_refusal" | grep -q 'wrong encryption key or corrupt' \
   && ! printf '%s' "$wrong_key_refusal" | grep -q 'no usable backup encryption key' \
   && printf '%s' "$plain_corrupt_refusal" | grep -q 'wrong encryption key or corrupt'; then
  pass "the decrypt probe names a missing key, and still reports wrong-key/corrupt otherwise"
else
  printf 'FAIL: probe messages wrong (missing=<<<%s>>> wrong=<<<%s>>> plain=<<<%s>>>)\n' \
    "$missing_key_refusal" "$wrong_key_refusal" "$plain_corrupt_refusal" >&2
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

# 6b. durable .destructive sentinel: never-heartbeated, written fail-closed BEFORE
#     DROP_STARTED=1 (a marker-write failure aborts before any destruction — nothing
#     is dropped, the disallow is still below) and before the disallow-ALTER, and
#     removed ONLY inside the clean lift block, so a SIGKILLed mid-swap restore stays
#     503 with no age gate (the heartbeat never re-touches it, so it survives it).
sf_destr_touch="$(sf_line 'touch "\$MAINTENANCE_DESTRUCTIVE"')"
if [ -n "$sf_dropstart" ] && [ -n "$sf_destr_touch" ] && [ -n "$sf_disallow" ] \
   && [ "$sf_destr_touch" -lt "$sf_dropstart" ] && [ "$sf_dropstart" -lt "$sf_disallow" ]; then
  pass "the .destructive sentinel is touched before DROP_STARTED and before the disallow"
else
  printf 'FAIL: destructive touch (%s) is not before DROP_STARTED (%s) and the disallow (%s)\n' \
    "$sf_destr_touch" "$sf_dropstart" "$sf_disallow" >&2
  fail=1
fi
# The durable marker is fail-closed: a write failure aborts via fail_before_destruction
# (nothing dropped yet — the disallow is below) rather than the old best-effort || true.
check "the durable .destructive marker is fail-closed on a write failure" \
  'fail_before_destruction "cannot write the durable maintenance marker'
if sed -n '/^restore_one_db_swap()/,/^}/p' "$RESTORE_SCRIPT" \
     | grep -qE 'touch "\$MAINTENANCE_DESTRUCTIVE" 2>/dev/null \|\| true'; then
  printf 'FAIL: the .destructive marker is still best-effort (|| true) — it must fail-closed before the swap\n' >&2
  fail=1
else
  pass "the .destructive marker is not the best-effort || true form"
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
# The .destructive sentinel is touched exactly ONCE — at the DROP-window mark,
# never in the heartbeat/watchdog loop: it has no age gate by design, so it must
# survive a SIGKILL that kills the heartbeat (the watchdog only READS it).
destr_touch_count="$(grep -Ec 'touch "\$MAINTENANCE_DESTRUCTIVE"' "$RESTORE_SCRIPT")"
if [ "$destr_touch_count" -eq 1 ]; then
  pass "the .destructive sentinel is touched exactly once (never re-touched by the heartbeat)"
else
  printf 'FAIL: .destructive is touched %s times (expected 1 — the heartbeat must never re-touch it)\n' \
    "$destr_touch_count" >&2
  fail=1
fi

# 6c. PRESENT-but-NO-CHECKSUMS manifest rejected BEFORE the DROP
#     (fail_before_destruction, nothing destroyed); the ABSENT-manifest WARN+proceed
#     back-compat path is unchanged. A present manifest that records no usable
#     schema version is refused unless the request acknowledges it (6e below).
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

# 6d. older-than-code backups now SELF-HEAL (the migration runner forward-migrates
#     them on the next app recreate), so RESTORE_OLDER is gone entirely — it no
#     longer holds maintenance, gates the lift, or prints a runbook line. Only a
#     newer-than-code backup is still refused before destruction.
if grep -q 'RESTORE_OLDER' "$RESTORE_SCRIPT"; then
  printf 'FAIL: RESTORE_OLDER still present (older backups must self-heal, not hold maintenance)\n' >&2
  fail=1
else
  pass "older backups self-heal (no RESTORE_OLDER hold/gate/message)"
fi
check "still refuses a newer-than-code backup before destruction" \
  'backup is newer than this deployment'

# 6e. A manifest that records no usable schema version (absent, or the 0 written by
#     older backups whose schema query failed) leaves the compat gate with nothing to
#     check. Restoring one is only allowed when the request says so explicitly, so the
#     gate block is run for real against fixture manifests with a stubbed refusal.
schema_gate_block="$(sed -n \
  '/MANIFEST_SCHEMA="\$(printf/,/^  if \[ "\$MANIFEST_AUTHENTICATED" = "1" \]; then$/p' \
  "$RESTORE_SCRIPT" | sed '$d')"
run_schema_gate() {
  # run_schema_gate <manifest json> <ALLOW_UNKNOWN_SCHEMA>
  bash -c '
    set -euo pipefail
    fail_before_destruction() { printf "REFUSED %s" "$1"; exit 9; }
    MANIFEST_CONTENT="$1"
    ALLOW_UNKNOWN_SCHEMA="$2"
    TIMESTAMP="20260801_120000"
    MIGRATIONS_DIR="$3"
    '"$schema_gate_block"'
    printf "PROCEEDED"
  ' _ "$1" "$2" "${SCRIPT_DIR}/../../db/migrations" 2>/dev/null
}
schema_zero_refused="$(run_schema_gate '{"schema_version":0}' 0 || true)"
schema_zero_allowed="$(run_schema_gate '{"schema_version":0}' 1 || true)"
schema_absent_refused="$(run_schema_gate '{"app_version":"1.0.0"}' 0 || true)"
schema_known_proceeds="$(run_schema_gate '{"schema_version":1}' 0 || true)"
if [ -n "$schema_gate_block" ] \
   && case "$schema_zero_refused" in REFUSED*) true ;; *) false ;; esac \
   && printf '%s' "$schema_zero_refused" | grep -q 'allow_unknown_schema' \
   && [ "$schema_zero_allowed" = "PROCEEDED" ] \
   && case "$schema_absent_refused" in REFUSED*) true ;; *) false ;; esac \
   && [ "$schema_known_proceeds" = "PROCEEDED" ]; then
  pass "a manifest without a usable schema version is refused unless the request acknowledges it"
else
  printf 'FAIL: unusable-schema gate wrong (zero=%s allowed=%s absent=%s known=%s)\n' \
    "$schema_zero_refused" "$schema_zero_allowed" "$schema_absent_refused" \
    "$schema_known_proceeds" >&2
  fail=1
fi

# === Rename-swap: preflight, swap-state file, sole gate, deterministic recovery =

# S1. Disk preflight runs BEFORE the first tmp CREATE (the destructive STEP-5 swap
#     call), sizing only the NEW space a restore consumes — both tmp DBs (fresh-DB
#     floor + gz x factor) + headroom — via df on the RO postgres_data mount. The
#     live DBs stay in place (renames are catalog-only), so their size is NOT added
#     to the free-space requirement (doing so would demand ~2x the live size free).
preflight_call_line="$(line_of '^preflight_disk_or_fail$')"
if [ -n "$preflight_call_line" ] && [ -n "$drop_call_line" ] && [ "$preflight_call_line" -lt "$drop_call_line" ]; then
  pass "disk preflight runs before the first tmp CREATE (the STEP-5 swap)"
else
  printf 'FAIL: preflight (%s) does not precede the destructive swap (%s)\n' \
    "$preflight_call_line" "$drop_call_line" >&2
  fail=1
fi
check "preflight requires only the two tmp DBs + headroom" \
  'req_kb=\$\(\( tmp_est_kb \+ headroom_kb \)\)'
if grep -Eq 'pg_database_size' "$RESTORE_SCRIPT"; then
  printf 'FAIL: preflight still queries pg_database_size (the live DB size must not be double-counted against free space)\n' >&2
  fail=1
else
  pass "preflight does not double-count the live DB size (free > tmp DBs + headroom only)"
fi
check "preflight reads free space via df on the postgres_data mount" \
  'df -Pk "\$POSTGRES_DATA_DIR"'
check "preflight is additive per-DB (fresh-DB floor + content factor)" 'content_factor=30'
check "preflight fails before destruction on a tight disk" \
  'insufficient disk for a safe restore'

# S2. Private swap-state file: written BEFORE each of the four transitions and read by
#     --recover to know which db to reconcile.
check "records swap state in the private lifecycle directory" \
  'SWAP_STATE_FILE="\$\{LOCK_DIR\}/restore-swap-state\.json"'
for ph in reload_tmp swapping_out swapping_in verified; do
  check "swap-state records the '$ph' phase" "write_swap_state \"\\\$db\" \"$ph\""
done

# S3. The SOLE post-swap gate is the SQL structural verify (schema_migrations
#     readable + jarvis auth tables), NEVER the app /health (which 503s under
#     maintenance and would revert a successful restore).
check "structural verify reads the migrations bookkeeping table" 'FROM schema_migrations'
check "structural verify asserts the jarvis auth tables exist" \
  "to_regclass\\('public.users'\\)"
if grep -vE '^[[:space:]]*#' "$RESTORE_SCRIPT" | grep -qE '/health|/livez|/readyz'; then
  printf 'FAIL: restore.sh polls an app health endpoint (the sole post-swap gate must be the SQL verify)\n' >&2
  fail=1
else
  pass "nothing polls the app /health (the SQL structural verify is the sole gate)"
fi

# S4. --recover runs ONLY the leftover-handler: it reconciles the recorded db and
#     exits WITHOUT consuming a request or running the full swap flow.
recover_block="$(sed -n '/= "--recover"/,/^fi/p' "$RESTORE_SCRIPT")"
if printf '%s' "$recover_block" | grep -q 'reconcile_leftover' \
   && ! printf '%s' "$recover_block" | grep -q 'restore_one_db_swap' \
   && ! printf '%s' "$recover_block" | grep -q 'REQUEST_FILE'; then
  pass "--recover only reconciles the recorded db (no full-flow, no request consume)"
else
  printf 'FAIL: the --recover branch runs more than the leftover-handler\n' >&2
  fail=1
fi
cleanup_block="$(sed -n '/^_cleanup()/,/^}/p' "$RESTORE_SCRIPT")"
if ! printf '%s' "$cleanup_block" | grep -q 'REQUEST_FILE' \
   && [ "$(grep -c 'rm -f -- "\$REQUEST_FILE"' "$RESTORE_SCRIPT")" -eq 1 ]; then
  pass "the main flow consumes each request once and EXIT cleanup cannot consume a later request"
else
  printf 'FAIL: restore requests must be consumed exactly once outside EXIT cleanup\n' >&2
  fail=1
fi
check "detects every outbound quarantine directory entry" \
  '\[ -e "\$OUTBOUND_QUARANTINE_SENTINEL" \] \|\| \[ -L "\$OUTBOUND_QUARANTINE_SENTINEL" \]'
check "publishes outbound quarantine without replacing an existing review" \
  'ln -- "\$tmp" "\$OUTBOUND_QUARANTINE_SENTINEL"'
check "flushes outbound quarantine data before reporting success" \
  'sync -d "\$OUTBOUND_QUARANTINE_SENTINEL"'
check "flushes the quarantine filesystem before reporting success" \
  'sync -f "\$\(dirname "\$OUTBOUND_QUARANTINE_SENTINEL"\)"'
if grep -q 'mv -T -- "\$tmp" "\$OUTBOUND_QUARANTINE_SENTINEL"' "$RESTORE_SCRIPT"; then
  printf 'FAIL: outbound quarantine publication can replace an existing review\n' >&2
  fail=1
else
  pass "outbound quarantine publication has no replacing move"
fi
check "--recover holds maintenance when the durable .destructive sentinel is present" \
  'if \[ -f "\$MAINTENANCE_DESTRUCTIVE" \]; then DROP_STARTED=1'

# S5. The revert path re-enables ALLOW_CONNECTIONS on the renamed-out pre_restore
#     BEFORE renaming it back (it inherited ALLOW_CONNECTIONS=false from the swap).
revert_fn="$(sed -n '/^revert_swap()/,/^}/p' "$RESTORE_SCRIPT")"
rv_enable="$(printf '%s\n' "$revert_fn" | grep -nE 'pre.*ALLOW_CONNECTIONS true' | head -1 | cut -d: -f1)"
rv_rename="$(printf '%s\n' "$revert_fn" | grep -nE 'RENAME TO .*db' | head -1 | cut -d: -f1)"
if [ -n "$rv_enable" ] && [ -n "$rv_rename" ] && [ "$rv_enable" -lt "$rv_rename" ]; then
  pass "revert re-enables ALLOW_CONNECTIONS on pre_restore before renaming it back"
else
  printf 'FAIL: revert does not re-enable connections before the rename-back (enable=%s rename=%s)\n' \
    "$rv_enable" "$rv_rename" >&2
  fail=1
fi

# S6. The safety pre-backup is forced past the maintenance skip-guard: the
#     restore's own .maintenance is already up, so the backup must be told to run.
check "safety pre-backup is forced past the maintenance skip-guard" 'export BACKUP_FORCE=1'

litellm_pause_line="$(line_of '^wait_for_litellm_quarantine[[:space:]]+\\$')"
maintenance_on_line="$(line_of '^touch "\$MAINTENANCE_SENTINEL"$')"
if [ -n "$maintenance_on_line" ] && [ -n "$litellm_pause_line" ] \
   && [ "$maintenance_on_line" -lt "$litellm_pause_line" ] \
   && [ "$litellm_pause_line" -lt "$safety_line" ]; then
  pass "LiteLLM stops after maintenance starts and before the safety backup"
else
  printf 'FAIL: LiteLLM stop proof (%s) must follow maintenance (%s) and precede backup (%s)\n' \
    "$litellm_pause_line" "$maintenance_on_line" "$safety_line" >&2
  fail=1
fi
check "LiteLLM stop proof requires two connection failures" \
  '\[ "\$failures" -ge 2 \]'
check "LiteLLM stop proof has a bounded default timeout" \
  'LITELLM_PAUSE_TIMEOUT_SECONDS:-60'

# 7. The maintenance sentinel is heartbeated for the whole run (re-touch loop) so
#    a long restore does not auto-expire mid-flight.
check "heartbeats the maintenance sentinel during the run" \
  'touch "\$MAINTENANCE_SENTINEL" 2>/dev/null'

# === Watchdog (bounded restore timeout) ======================================
# A background deadline aborts a hung restore instead of holding the stack at 503
# forever. It cannot read the parent's live DROP_STARTED, so it fixes the
# maintenance sentinels ITSELF — lifting .maintenance ONLY when nothing was
# destroyed (.destructive absent) — then signals the main process so the single
# EXIT trap writes the terminal status.
check "computes a bounded restore deadline" 'RESTORE_DEADLINE='
check "defaults the restore time limit to 3600s (never fires on a slow safety backup)" \
  'RESTORE_MAX_SECONDS:-3600'
check "captures the main PID so the watchdog can signal it" 'MAIN_PID=\$\$'
check "the watchdog drops a private timeout marker on deadline" \
  ': > "\$RESTORE_TIMEOUT_FILE"'
check "the watchdog signals the main process on deadline" 'kill "\$MAIN_PID"'
check "routes SIGTERM into the single EXIT trap" "trap 'exit 143' TERM"
check "routes SIGINT into the single EXIT trap" "trap 'exit 130' INT"
check "_cleanup words a timeout distinctly from a mid-reload failure" \
  'restore exceeded its time limit'
check "clears a stale private timeout marker at the start of a run" \
  'rm -f "\$RESTORE_TIMEOUT_FILE"'

# The watchdog lifts .maintenance ONLY when .destructive is absent. This is the
# SECOND (later-in-file) maintenance removal; the first is the EXIT-trap clean
# lift gate (section 6).
wd_rm_line="$(grep -nE 'rm -f "\$MAINTENANCE_SENTINEL"' "$RESTORE_SCRIPT" | tail -1 | cut -d: -f1)"
wd_guard_line="$(line_of '\[ ! -f "\$MAINTENANCE_DESTRUCTIVE" \]')"
if [ -n "$wd_rm_line" ] && [ -n "$wd_guard_line" ] \
   && [ "$wd_guard_line" -lt "$wd_rm_line" ] \
   && [ "$((wd_rm_line - wd_guard_line))" -le 2 ]; then
  pass "the watchdog lifts .maintenance only when .destructive is absent"
else
  printf 'FAIL: watchdog maintenance rm (%s) is not guarded by the .destructive-absent check (%s)\n' \
    "$wd_rm_line" "$wd_guard_line" >&2
  fail=1
fi

# Behavioral: single-source the deadline-decision block from restore.sh and run
# both branches (no live sidecar, no 60s sleep). With .destructive ABSENT it must
# lift .maintenance + drop the private timeout marker; with it PRESENT it must HOLD
# .maintenance (never re-expose a destroyed DB).
# Anchor on the watchdog's unique timeout marker. Other safe file-creation
# helpers may also use `: >`; selecting the first one made this behavioral test
# silently execute an unrelated lifecycle-lock branch.
wd_block="$(sed -n \
  '/^[[:space:]]*: > "\$RESTORE_TIMEOUT_FILE"/,/^[[:space:]]*fi$/p' \
  "$RESTORE_SCRIPT")"
run_wd() {
  # $1 = "destroyed" -> pre-create .destructive; anything else -> absent.
  local d; d="$(mktemp -d)"
  mkdir -p "${d}/.lifecycle"
  touch "${d}/.maintenance"
  [ "$1" = "destroyed" ] && touch "${d}/.destructive"
  TRIGGER_DIR="$d" MAINTENANCE_SENTINEL="${d}/.maintenance" \
  MAINTENANCE_DESTRUCTIVE="${d}/.destructive" \
  RESTORE_TIMEOUT_FILE="${d}/.lifecycle/restore-timeout" bash -c '
    set -euo pipefail
    '"$wd_block"'
  ' 2>/dev/null
  local maint=absent timeout=absent
  [ -f "${d}/.maintenance" ] && maint=present
  [ -f "${d}/.lifecycle/restore-timeout" ] && timeout=present
  printf '%s %s' "$maint" "$timeout"
  rm -rf "$d"
}
wd_clean="$(run_wd clean)"
wd_destroyed="$(run_wd destroyed)"
if [ -n "$wd_block" ] && [ "$wd_clean" = "absent present" ] && [ "$wd_destroyed" = "present present" ]; then
  pass "watchdog deadline branch: lifts .maintenance when nothing destroyed, holds it when .destructive present, always drops .restore_timeout"
else
  printf 'FAIL: watchdog deadline branch behaved wrong (clean=%s destroyed=%s)\n' \
    "$wd_clean" "$wd_destroyed" >&2
  fail=1
fi

# 8. exit 0 after a recorded terminal failure. Non-zero exits are allowed only
#    inside guarded helper subshells, the embedded Perl parser, and signal traps;
#    there is no unguarded script-level exit that could crash-loop the sidecar.
check "fails before destruction with exit 0" 'fail_before_destruction\(\)'
check "fails during/after the drop with exit 0" 'step5_fail\(\)'
unexpected_nonzero="$(awk '
  /^swap_restored_pdfs\(\)|^recover_pdf_swap\(\)|^qdrant_http_body\(\)/ { guarded=1 }
  guarded && /^}/ { guarded=0; next }
  /trap '\''exit (130|143)'\''/ { next }
  /exit[[:space:]]+[1-9]/ && !guarded { print NR ":" $0 }
' "$RESTORE_SCRIPT")"
if [ -z "$unexpected_nonzero" ]; then
  pass "no unguarded bash-level non-zero exit (terminal failures exit 0; sidecar never crash-restarts)"
else
  printf 'FAIL: an unguarded bash-level non-zero exit exists: %s\n' "$unexpected_nonzero" >&2
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

# B2. valid_archive_name accepts every archive role, rejects path-seps / .. / junk.
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
  "pdfs_20260626_120000.tar.gz.enc" \
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
  pass "valid_archive_name accepts all archive roles and rejects path-seps/../junk"
else
  fail=1
fi

# B3. write_status emits the RestoreStatus API shape as valid JSON (5 named
#     steps, escaped error string, all required keys present).
if command -v python3 >/dev/null 2>&1; then
  st_dir="$(mktemp -d)"
  st_out="${st_dir}/status.json"
  STATUS_FILE="$st_out" bash -c '
    set -euo pipefail
    STATE="running"; CURRENT_STEP="Restoring database"; ERROR="boom \"q\" \\ x"
    SAFETY_BACKUP_TS="20260626_120000"; STARTED_AT="2026-06-26T12:00:00+00:00"
    FINISHED_AT=""; DROP_STARTED=1; MANUAL_STEPS_REQUIRED=1; PHASE="reload-db"
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
          "started_at", "finished_at", "error", "manual_steps_required", "phase"):
    assert k in d, k
assert d["safety_backup_ts"] == "20260626_120000"
assert d["error"] == 'boom "q" \\ x', repr(d["error"])
assert d["manual_steps_required"] is True, d["manual_steps_required"]
assert d["phase"] == "reload-db", d["phase"]
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
# Inbox recovery reads an operator-provided archive set and one-time key from the
# writable restore_inbox volume. It materializes secrets in a temporary directory
# outside /secrets, preserves the target host's database password, and securely
# removes the key and plaintext directory on every exit.
BACKUP_SCRIPT="${SCRIPT_DIR}/../backup.sh"

# The request source defaults to local and rejects unsupported values.
check "parses the request source field" '"source"'
check "defaults the restore source to local when absent" 'SOURCE="\$\{SOURCE_RAW:-local\}"'
check "rejects an unsupported source (fail-safe, not silent-local)" 'expected local or inbox'
check "off-host (inbox) restore requires a manifest so compat + integrity are armed" \
  'off-host restore requires manifest_'

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

# I4. The target host's postgres role and its mounted password file are one local
#     infrastructure identity. Restoring neither side avoids an uncloseable crash
#     window where one changes before the other.
if grep -vE '^[[:space:]]*#' "$RESTORE_SCRIPT" \
    | grep -Eq 'ALTER[[:space:]]+ROLE|OLD_PG_PW'; then
  printf 'FAIL: off-host restore still mutates the target postgres role password\n' >&2
  fail=1
else
  pass "off-host restore preserves the target postgres role password"
fi

# I5. Inbox-only handling remains guarded by source=inbox.
check "guards inbox-only handling behind source=inbox" \
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

# I9. A clean restore now lifts maintenance regardless of source: the inbox path
#     installs only restored data keys and writes the rotation marker, so services
#     reload keys while every target-host credential stays authoritative.
check "lifts maintenance on any clean restore (inbox self-recovers)" \
  '\[ "\$RESTORE_CLEAN" = "1" \] \|\| \[ "\$DROP_STARTED" = "0" \]'
# Scoped to the cleanup handler that owns the gate. Elsewhere in the script an
# off-host source is legitimately excluded — the unsigned-set override is
# same-host only — so a whole-file grep would read that refusal as this hold.
if sed -n '/^_cleanup()/,/^}/p' "$RESTORE_SCRIPT" | grep -Eq '!= "inbox"'; then
  printf 'FAIL: the lift gate still excludes inbox (the self-restart contract removed that hold)\n' >&2
  fail=1
else
  pass "the lift gate no longer excludes inbox (old operator hold removed)"
fi

# I9b. Exercise the real staging and installation helpers. Current archives must
# contain exactly the three data keys. Authenticated historical archives may carry
# obsolete host credentials, but only the three data keys may be extracted or
# installed. Non-regular members fail before installation.
dk_dir="$(mktemp -d)"
dk_exact="${dk_dir}/exact"; dk_legacy="${dk_dir}/legacy"
dk_missing="${dk_dir}/missing"; dk_symlink="${dk_dir}/symlink"
dk_hardlink="${dk_dir}/hardlink"
mkdir -p "$dk_exact" "$dk_legacy" "$dk_missing" "$dk_symlink" "$dk_hardlink"
for dir in "$dk_exact" "$dk_legacy" "$dk_missing" "$dk_symlink" "$dk_hardlink"; do
  printf 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=' > "${dir}/jarvis_config_key.txt"
  printf 'model-hmac-key-0123456789abcdefX' > "${dir}/jarvis_model_hmac_key.txt"
  printf 'litellm-salt-key' > "${dir}/litellm_salt_key.txt"
done
rm -f "${dk_missing}/litellm_salt_key.txt"
printf 'archived-postgres-password' > "${dk_legacy}/postgres_password.txt"
printf 'archived-api-key' > "${dk_legacy}/jarvis_api_key.txt"
rm -f "${dk_symlink}/jarvis_model_hmac_key.txt"
ln -s /etc/passwd "${dk_symlink}/jarvis_model_hmac_key.txt"
printf 'model-hmac-key-0123456789abcdefX' > "${dk_hardlink}/hardlink-source"
rm -f "${dk_hardlink}/jarvis_model_hmac_key.txt"
ln "${dk_hardlink}/hardlink-source" "${dk_hardlink}/jarvis_model_hmac_key.txt"
tar -czf "${dk_dir}/exact.tar.gz" -C "$dk_exact" \
  jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt
tar -czf "${dk_dir}/legacy.tar.gz" -C "$dk_legacy" \
  jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt \
  postgres_password.txt jarvis_api_key.txt
tar -czf "${dk_dir}/missing.tar.gz" -C "$dk_missing" \
  jarvis_config_key.txt jarvis_model_hmac_key.txt
tar -czf "${dk_dir}/symlink.tar.gz" -C "$dk_symlink" \
  jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt
tar -czf "${dk_dir}/hardlink.tar.gz" -C "$dk_hardlink" \
  hardlink-source jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt

run_data_key_stage() {
  local archive="$1" exact="$2" work="$3"
  mkdir -p "${work}/inbox" "${work}/host" "${work}/trigger"
  RESTORE_INBOX_DIR="${work}/inbox" HOST_SECRETS_DIR="${work}/host" \
    BACKUP_TRIGGER_DIR="${work}/trigger" bash -c '
      set -euo pipefail
      source "$1" --functions-only
      stage_restored_data_keys "$2" "$3"
    ' _ "$RESTORE_SCRIPT" "$archive" "$exact" 2>/dev/null
}

dk_exact_rc=0; run_data_key_stage "${dk_dir}/exact.tar.gz" 1 "${dk_dir}/work-exact" \
  || dk_exact_rc=$?
dk_legacy_rc=0; run_data_key_stage "${dk_dir}/legacy.tar.gz" 0 "${dk_dir}/work-legacy" \
  || dk_legacy_rc=$?
dk_extra_rc=0; run_data_key_stage "${dk_dir}/legacy.tar.gz" 1 "${dk_dir}/work-extra" \
  || dk_extra_rc=$?
dk_missing_rc=0; run_data_key_stage "${dk_dir}/missing.tar.gz" 1 "${dk_dir}/work-missing" \
  || dk_missing_rc=$?
dk_symlink_rc=0; run_data_key_stage "${dk_dir}/symlink.tar.gz" 0 "${dk_dir}/work-symlink" \
  || dk_symlink_rc=$?
dk_hardlink_rc=0; run_data_key_stage "${dk_dir}/hardlink.tar.gz" 0 "${dk_dir}/work-hardlink" \
  || dk_hardlink_rc=$?
if [ "$dk_exact_rc" -eq 0 ] && [ "$dk_legacy_rc" -eq 0 ] \
   && [ "$dk_extra_rc" -ne 0 ] && [ "$dk_missing_rc" -ne 0 ] \
   && [ "$dk_symlink_rc" -ne 0 ] && [ "$dk_hardlink_rc" -ne 0 ] \
   && [ ! -e "${dk_dir}/work-legacy/inbox/.secrets-staging/postgres_password.txt" ] \
   && [ ! -e "${dk_dir}/work-legacy/inbox/.secrets-staging/jarvis_api_key.txt" ]; then
  pass "data-key staging enforces the current exact set and safely filters historical archives"
else
  printf 'FAIL: data-key staging contract wrong (exact=%s legacy=%s extra=%s missing=%s symlink=%s hardlink=%s)\n' \
    "$dk_exact_rc" "$dk_legacy_rc" "$dk_extra_rc" "$dk_missing_rc" \
    "$dk_symlink_rc" "$dk_hardlink_rc" >&2
  fail=1
fi

dk_install_work="${dk_dir}/install"
mkdir -p "${dk_install_work}/inbox" "${dk_install_work}/host" \
  "${dk_install_work}/trigger" "${dk_install_work}/backups/.lifecycle"
for name in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
  cp "${dk_exact}/${name}" "${dk_install_work}/host/${name}"
done
for name in postgres_password.txt qdrant_api_key.txt jarvis_api_key.txt \
            litellm_master_key.txt smtp_password.txt telegram_bot_token.txt \
            backup_encrypt_key.txt future_target_credential.txt; do
  printf 'target-%s' "$name" > "${dk_install_work}/host/${name}"
done
dk_install_rc=0
RESTORE_INBOX_DIR="${dk_install_work}/inbox" HOST_SECRETS_DIR="${dk_install_work}/host" \
  BACKUP_TRIGGER_DIR="${dk_install_work}/trigger" BACKUP_DIR="${dk_install_work}/backups" bash -c '
    set -euo pipefail
    source "$1" --functions-only
    stage_restored_data_keys "$2" 1
    install_restored_data_keys
  ' _ "$RESTORE_SCRIPT" "${dk_dir}/exact.tar.gz" 2>/dev/null || dk_install_rc=$?
dk_credentials_unchanged=1
for name in postgres_password.txt qdrant_api_key.txt jarvis_api_key.txt \
            litellm_master_key.txt smtp_password.txt telegram_bot_token.txt \
            backup_encrypt_key.txt future_target_credential.txt; do
  [ "$(cat "${dk_install_work}/host/${name}")" = "target-${name}" ] \
    || dk_credentials_unchanged=0
done
if [ "$dk_install_rc" -eq 0 ] && [ "$dk_credentials_unchanged" -eq 1 ] \
   && cmp -s "${dk_exact}/jarvis_config_key.txt" "${dk_install_work}/host/jarvis_config_key.txt" \
   && cmp -s "${dk_exact}/jarvis_model_hmac_key.txt" "${dk_install_work}/host/jarvis_model_hmac_key.txt" \
   && cmp -s "${dk_exact}/litellm_salt_key.txt" "${dk_install_work}/host/litellm_salt_key.txt" \
   && [ -s "${dk_install_work}/trigger/.secrets_rotated" ]; then
  pass "data-key installation replaces only three keys and then publishes the reload marker"
else
  printf 'FAIL: data-key installation changed a target credential or published an incomplete set\n' >&2
  fail=1
fi

dk_partial="${dk_dir}/partial"
mkdir -p "${dk_partial}/inbox" "${dk_partial}/host" "${dk_partial}/trigger" \
  "${dk_partial}/backups/.lifecycle"
cp "${dk_exact}/jarvis_config_key.txt" "${dk_partial}/host/jarvis_config_key.txt"
cp "${dk_exact}/litellm_salt_key.txt" "${dk_partial}/host/litellm_salt_key.txt"
printf 'do-not-change' > "${dk_partial}/symlink-target"
ln -s "${dk_partial}/symlink-target" "${dk_partial}/host/jarvis_model_hmac_key.txt"
dk_partial_rc=0
RESTORE_INBOX_DIR="${dk_partial}/inbox" HOST_SECRETS_DIR="${dk_partial}/host" \
  BACKUP_TRIGGER_DIR="${dk_partial}/trigger" BACKUP_DIR="${dk_partial}/backups" bash -c '
    set -euo pipefail
    source "$1" --functions-only
    stage_restored_data_keys "$2" 1
    install_restored_data_keys
  ' _ "$RESTORE_SCRIPT" "${dk_dir}/exact.tar.gz" 2>/dev/null || dk_partial_rc=$?
if [ "$dk_partial_rc" -ne 0 ] && [ ! -e "${dk_partial}/trigger/.secrets_rotated" ] \
   && [ "$(cat "${dk_partial}/symlink-target")" = "do-not-change" ]; then
  pass "data-key installation never publishes the reload marker after a partial copy"
else
  printf 'FAIL: data-key installation marked a partial or unsafe copy complete\n' >&2
  fail=1
fi
rm -rf "$dk_dir"

# I10. PDF staging accepts a complete flat numeric set (including empty), and
# rejects names or member types that could escape or corrupt the library swap.
pdf_dir="$(mktemp -d)"
mkdir -p "${pdf_dir}/good" "${pdf_dir}/bad-name" "${pdf_dir}/bad-link"
printf 'PDF-ONE' > "${pdf_dir}/good/1.pdf"
printf 'PDF-FORTY-TWO' > "${pdf_dir}/good/42.pdf"
printf 'not-a-pdf-id' > "${pdf_dir}/bad-name/notes.txt"
ln -s /etc/passwd "${pdf_dir}/bad-link/2.pdf"
tar -czf "${pdf_dir}/good.tar.gz" -C "${pdf_dir}/good" 1.pdf 42.pdf
tar -czf "${pdf_dir}/empty.tar.gz" --files-from /dev/null
tar -czf "${pdf_dir}/bad-name.tar.gz" -C "${pdf_dir}/bad-name" notes.txt
tar -czf "${pdf_dir}/bad-link.tar.gz" -C "${pdf_dir}/bad-link" 2.pdf
run_pdf_stage() {
  local archive="$1" run_id="$2" storage="$3"
  mkdir -p "$storage"
  PDF_STORAGE_DIR="$storage" bash -c '
    set -euo pipefail
    source "$1" --functions-only
    stage_restored_pdfs "$2" "$3"
  ' _ "$RESTORE_SCRIPT" "$archive" "$run_id" 2>/dev/null
}
pdf_good_run="11111111111111111111111111111111"
pdf_empty_run="22222222222222222222222222222222"
pdf_bad_name_run="33333333333333333333333333333333"
pdf_bad_link_run="44444444444444444444444444444444"
pdf_good_storage="${pdf_dir}/storage-good"
pdf_empty_storage="${pdf_dir}/storage-empty"
pdf_bad_name_storage="${pdf_dir}/storage-bad-name"
pdf_bad_link_storage="${pdf_dir}/storage-bad-link"
pdf_good_rc=0; run_pdf_stage "${pdf_dir}/good.tar.gz" "$pdf_good_run" "$pdf_good_storage" \
  || pdf_good_rc=$?
pdf_empty_rc=0; run_pdf_stage "${pdf_dir}/empty.tar.gz" "$pdf_empty_run" "$pdf_empty_storage" \
  || pdf_empty_rc=$?
pdf_bad_name_rc=0; run_pdf_stage "${pdf_dir}/bad-name.tar.gz" "$pdf_bad_name_run" "$pdf_bad_name_storage" \
  || pdf_bad_name_rc=$?
pdf_bad_link_rc=0; run_pdf_stage "${pdf_dir}/bad-link.tar.gz" "$pdf_bad_link_run" "$pdf_bad_link_storage" \
  || pdf_bad_link_rc=$?
if [ "$pdf_good_rc" -eq 0 ] && [ "$pdf_empty_rc" -eq 0 ] \
   && [ "$pdf_bad_name_rc" -ne 0 ] && [ "$pdf_bad_link_rc" -ne 0 ] \
   && cmp -s "${pdf_dir}/good/1.pdf" "${pdf_good_storage}/.restore-stage-${pdf_good_run}/1.pdf" \
   && cmp -s "${pdf_dir}/good/42.pdf" "${pdf_good_storage}/.restore-stage-${pdf_good_run}/42.pdf" \
   && [ "$(wc -l < "${pdf_good_storage}/.restore-stage-${pdf_good_run}/.inventory.tsv")" -eq 2 ] \
   && [ ! -s "${pdf_empty_storage}/.restore-stage-${pdf_empty_run}/.inventory.tsv" ]; then
  pass "PDF staging accepts exact numeric sets and rejects unsafe archive members"
else
  printf 'FAIL: PDF staging contract wrong (good=%s empty=%s bad-name=%s bad-link=%s)\n' \
    "$pdf_good_rc" "$pdf_empty_rc" "$pdf_bad_name_rc" "$pdf_bad_link_rc" >&2
  fail=1
fi

run_pdf_consent() {
  local request="$1" authenticated="$2" legacy="$3"
  bash -c '
    set -euo pipefail
    source "$1" --functions-only
    if parse_allow_missing_pdfs_request "$2"; then parsed=OK; else parsed=BAD; fi
    MANIFEST_AUTHENTICATED="$3"; MANIFEST_LEGACY="$4"
    if missing_pdf_restore_is_authorized; then authorized=YES; else authorized=NO; fi
    printf "%s:%s:%s" "$parsed" "$ALLOW_MISSING_PDFS" "$authorized"
  ' _ "$RESTORE_SCRIPT" "$request" "$authenticated" "$legacy" 2>/dev/null
}
pdf_consent_absent="$(run_pdf_consent '{}' 1 1)"
pdf_consent_true="$(run_pdf_consent '{"allow_missing_pdfs":true}' 1 1)"
pdf_consent_false="$(run_pdf_consent '{"allow_missing_pdfs":false}' 1 1)"
pdf_consent_unsigned="$(run_pdf_consent '{"allow_missing_pdfs":true}' 0 1)"
pdf_consent_current="$(run_pdf_consent '{"allow_missing_pdfs":true}' 1 0)"
pdf_consent_duplicate="$(run_pdf_consent \
  '{"allow_missing_pdfs":true,"allow_missing_pdfs":false}' 1 1)"
pdf_consent_string="$(run_pdf_consent '{"allow_missing_pdfs":"true"}' 1 1)"
if [ "$pdf_consent_absent" = "OK:0:NO" ] \
   && [ "$pdf_consent_true" = "OK:1:YES" ] \
   && [ "$pdf_consent_false" = "OK:0:NO" ] \
   && [ "$pdf_consent_unsigned" = "OK:1:NO" ] \
   && [ "$pdf_consent_current" = "OK:1:NO" ] \
   && [ "$pdf_consent_duplicate" = "BAD:0:NO" ] \
   && [ "$pdf_consent_string" = "BAD:0:NO" ]; then
  pass "missing PDFs require explicit consent plus an authenticated strict legacy manifest"
else
  printf 'FAIL: missing-PDF consent gate accepted an unsafe request or manifest state\n' >&2
  fail=1
fi
rm -rf "$pdf_dir"

# I10b. The unknown-schema acknowledgement is parsed with the same strictness as the
#       missing-PDF one: absent and false are safe defaults, duplicate or non-boolean
#       values are refused rather than read as consent.
run_schema_consent() {
  bash -c '
    set -euo pipefail
    source "$1" --functions-only
    if parse_allow_unknown_schema_request "$2"; then parsed=OK; else parsed=BAD; fi
    printf "%s:%s" "$parsed" "$ALLOW_UNKNOWN_SCHEMA"
  ' _ "$RESTORE_SCRIPT" "$1" 2>/dev/null
}
if [ "$(run_schema_consent '{}')" = "OK:0" ] \
   && [ "$(run_schema_consent '{"allow_unknown_schema":true}')" = "OK:1" ] \
   && [ "$(run_schema_consent '{"allow_unknown_schema":false}')" = "OK:0" ] \
   && [ "$(run_schema_consent '{"allow_unknown_schema":true,"allow_unknown_schema":false}')" = "BAD:0" ] \
   && [ "$(run_schema_consent '{"allow_unknown_schema":"true"}')" = "BAD:0" ]; then
  pass "the unknown-schema acknowledgement defaults to off and refuses malformed values"
else
  printf 'FAIL: unknown-schema acknowledgement parsing accepted an unsafe request\n' >&2
  fail=1
fi

# I11. The decrypted plaintext secret bundle is shredded (not just rm'd) on exit.
check "shreds the staged plaintext secret files (not a bare rm)" \
  'find "\$SECRETS_STAGING" -type f -exec shred -u'

# I12. Cleanup fires whenever the operator key / staging exist — not only when
#      SOURCE resolved to inbox — so a malformed source field still shreds the key.
check "shreds the operator key even if the source field was malformed" \
  '\[ -e "\$OPERATOR_KEYFILE" \]'

# I13. --inbox-manifest is a READ-ONLY inventory pass: it writes a SANITIZED manifest
#      (names/booleans only) the app's GET /inbox lists, and MANIFEST_MODE short-circuits
#      the EXIT trap so it never consumes the restore request, writes a status, or shreds
#      the operator key. Static structure + a behavioral run against a seeded fake inbox.
check "defines the --inbox-manifest inventory branch" '\[ "\$\{1:-\}" = "--inbox-manifest" \]'
check "the inbox-manifest branch sets MANIFEST_MODE" 'MANIFEST_MODE=1'
check "the EXIT trap short-circuits in MANIFEST_MODE (no consume/status/shred)" \
  '\[ "\$MANIFEST_MODE" = "1" \] && exit 0'
check "the manifest emits only names/booleans (no path/key fields)" \
  '"timestamp":"%s","complete":%s,"has_pdfs":%s,"legacy_missing_pdfs":%s,"has_secrets":%s,"has_key":%s'

if command -v python3 >/dev/null 2>&1; then
  im_dir="$(mktemp -d)"
  im_inbox="${im_dir}/inbox"
  im_trig="${im_dir}/trig"
  mkdir -p "$im_inbox" "$im_trig"
  # Current unencrypted sets need both databases, PDFs, and a manifest; they may
  # omit the data-key archive. A missing-PDF legacy set is complete only when its
  # strict pre-v1.2 manifest and exact inventory authenticate with the supplied key.
  current_ts="20260701_030000"
  printf 'CURRENT-J' > "${im_inbox}/jarvis_${current_ts}.sql.gz"
  printf 'CURRENT-L' > "${im_inbox}/litellm_${current_ts}.sql.gz"
  printf 'CURRENT-P' > "${im_inbox}/pdfs_${current_ts}.tar.gz"
  printf '{"timestamp":"%s","run_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","archives":[]}' \
    "$current_ts" > "${im_inbox}/manifest_${current_ts}.json"

  legacy_ts="20260630_020000"
  unsigned_legacy_ts="20260629_010000"
  for ts in "$legacy_ts" "$unsigned_legacy_ts"; do
    printf 'LEGACY-J-%s' "$ts" > "${im_inbox}/jarvis_${ts}.sql.gz.enc"
    printf 'LEGACY-L-%s' "$ts" > "${im_inbox}/litellm_${ts}.sql.gz.enc"
    printf 'LEGACY-S-%s' "$ts" > "${im_inbox}/secrets_${ts}.tar.gz.enc"
    legacy_j_sha="$(sha256sum "${im_inbox}/jarvis_${ts}.sql.gz.enc" | cut -d' ' -f1)"
    legacy_l_sha="$(sha256sum "${im_inbox}/litellm_${ts}.sql.gz.enc" | cut -d' ' -f1)"
    legacy_s_sha="$(sha256sum "${im_inbox}/secrets_${ts}.tar.gz.enc" | cut -d' ' -f1)"
    legacy_j_size="$(stat -c%s "${im_inbox}/jarvis_${ts}.sql.gz.enc")"
    legacy_l_size="$(stat -c%s "${im_inbox}/litellm_${ts}.sql.gz.enc")"
    legacy_s_size="$(stat -c%s "${im_inbox}/secrets_${ts}.tar.gz.enc")"
    printf '{"timestamp":"%s","app_version":"1.1.3","schema_version":102,"created_at":"2026-06-30T02:00:00+00:00","archives":[{"filename":"jarvis_%s.sql.gz.enc","sha256":"%s","size_bytes":%s},{"filename":"litellm_%s.sql.gz.enc","sha256":"%s","size_bytes":%s},{"filename":"secrets_%s.tar.gz.enc","sha256":"%s","size_bytes":%s}]}' \
      "$ts" "$ts" "$legacy_j_sha" "$legacy_j_size" \
      "$ts" "$legacy_l_sha" "$legacy_l_size" \
      "$ts" "$legacy_s_sha" "$legacy_s_size" \
      > "${im_inbox}/manifest_${ts}.json"
  done

  incomplete_ts="20260702_040000"
  printf 'INCOMPLETE-J' > "${im_inbox}/jarvis_${incomplete_ts}.sql.gz"
  printf 'INCOMPLETE-L' > "${im_inbox}/litellm_${incomplete_ts}.sql.gz"
  printf 'INCOMPLETE-P' > "${im_inbox}/pdfs_${incomplete_ts}.tar.gz"
  signed_current_ts="20260703_050000"
  printf 'SIGNED-CURRENT-J' > "${im_inbox}/jarvis_${signed_current_ts}.sql.gz.enc"
  printf 'SIGNED-CURRENT-L' > "${im_inbox}/litellm_${signed_current_ts}.sql.gz.enc"
  printf 'SIGNED-CURRENT-S' > "${im_inbox}/secrets_${signed_current_ts}.tar.gz.enc"
  current_j_sha="$(sha256sum "${im_inbox}/jarvis_${signed_current_ts}.sql.gz.enc" | cut -d' ' -f1)"
  current_l_sha="$(sha256sum "${im_inbox}/litellm_${signed_current_ts}.sql.gz.enc" | cut -d' ' -f1)"
  current_s_sha="$(sha256sum "${im_inbox}/secrets_${signed_current_ts}.tar.gz.enc" | cut -d' ' -f1)"
  current_j_size="$(stat -c%s "${im_inbox}/jarvis_${signed_current_ts}.sql.gz.enc")"
  current_l_size="$(stat -c%s "${im_inbox}/litellm_${signed_current_ts}.sql.gz.enc")"
  current_s_size="$(stat -c%s "${im_inbox}/secrets_${signed_current_ts}.tar.gz.enc")"
  printf '{"timestamp":"%s","run_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","archives":[{"filename":"jarvis_%s.sql.gz.enc","sha256":"%s","size_bytes":%s},{"filename":"litellm_%s.sql.gz.enc","sha256":"%s","size_bytes":%s},{"filename":"secrets_%s.tar.gz.enc","sha256":"%s","size_bytes":%s}]}' \
    "$signed_current_ts" "$signed_current_ts" "$current_j_sha" "$current_j_size" \
    "$signed_current_ts" "$current_l_sha" "$current_l_size" \
    "$signed_current_ts" "$current_s_sha" "$current_s_size" \
    > "${im_inbox}/manifest_${signed_current_ts}.json"
  : > "${im_inbox}/not-an-archive.txt"
  printf 'restore-inbox-test-key' > "${im_inbox}/operator_key"
  im_derived="$(openssl dgst -sha256 -hmac 'jarvis-manifest-v1' -r \
    < "${im_inbox}/operator_key" | cut -d' ' -f1)"
  openssl dgst -sha256 -mac HMAC -macopt "hexkey:${im_derived}" -r \
    < "${im_inbox}/manifest_${legacy_ts}.json" | cut -d' ' -f1 \
    > "${im_inbox}/manifest_${legacy_ts}.json.hmac"
  openssl dgst -sha256 -mac HMAC -macopt "hexkey:${im_derived}" -r \
    < "${im_inbox}/manifest_${signed_current_ts}.json" | cut -d' ' -f1 \
    > "${im_inbox}/manifest_${signed_current_ts}.json.hmac"
  # A pending restore request the inventory pass must NOT consume.
  printf '{"timestamp":"%s","confirm":"RESTORE"}' "$current_ts" \
    > "${im_trig}/.restore_request.json"
  im_rc=0
  RESTORE_INBOX_DIR="$im_inbox" BACKUP_TRIGGER_DIR="$im_trig" \
    bash "$RESTORE_SCRIPT" --inbox-manifest >/dev/null 2>&1 || im_rc=$?
  if python3 - "${im_trig}/.inbox_manifest.json" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
d = json.loads(raw)
by = {e["timestamp"]: e for e in d}
assert set(by) == {
    "20260701_030000", "20260630_020000", "20260629_010000", "20260702_040000",
    "20260703_050000"
}, by
expected_keys = {
    "timestamp", "complete", "has_pdfs", "legacy_missing_pdfs", "has_secrets", "has_key"
}
assert all(set(entry) == expected_keys for entry in d), d
assert by["20260701_030000"] == {
    "timestamp": "20260701_030000", "complete": True, "has_pdfs": True,
    "legacy_missing_pdfs": False, "has_secrets": False, "has_key": True
}, by
assert by["20260630_020000"] == {
    "timestamp": "20260630_020000", "complete": True, "has_pdfs": False,
    "legacy_missing_pdfs": True, "has_secrets": True, "has_key": True
}, by
assert by["20260629_010000"]["complete"] is False, by
assert by["20260629_010000"]["legacy_missing_pdfs"] is False, by
assert by["20260702_040000"]["complete"] is False, by
assert by["20260702_040000"]["has_pdfs"] is True, by
assert by["20260703_050000"]["complete"] is False, by
assert by["20260703_050000"]["legacy_missing_pdfs"] is False, by
# Sanitized: no path or key material anywhere in the JSON.
assert "operator_key" not in raw and "restore-inbox-test-key" not in raw and "/" not in raw, raw
PY
  then
    pass "--inbox-manifest distinguishes current PDF sets from authenticated PDF-less legacy sets"
  else
    printf 'FAIL: --inbox-manifest manifest wrong or leaked a path/key\n' >&2
    fail=1
  fi
  if [ "$im_rc" -ne 0 ]; then
    printf 'FAIL: --inbox-manifest exited non-zero (%s)\n' "$im_rc" >&2; fail=1
  fi
  if [ ! -f "${im_trig}/.restore_request.json" ]; then
    printf 'FAIL: --inbox-manifest consumed the restore request sentinel\n' >&2; fail=1
  fi
  if [ ! -f "${im_inbox}/operator_key" ]; then
    printf 'FAIL: --inbox-manifest shredded the operator key\n' >&2; fail=1
  fi
  if [ -f "${im_trig}/.restore_status.json" ]; then
    printf 'FAIL: --inbox-manifest wrote a restore status file\n' >&2; fail=1
  fi
  # Empty inbox -> [] (exit 0).
  rm -f "${im_inbox:?}"/*
  RESTORE_INBOX_DIR="$im_inbox" BACKUP_TRIGGER_DIR="$im_trig" \
    bash "$RESTORE_SCRIPT" --inbox-manifest >/dev/null 2>&1 || true
  if [ "$(cat "${im_trig}/.inbox_manifest.json")" = "[]" ]; then
    pass "--inbox-manifest writes [] for an empty inbox"
  else
    printf 'FAIL: --inbox-manifest did not write [] for an empty inbox (got: %s)\n' \
      "$(cat "${im_trig}/.inbox_manifest.json")" >&2
    fail=1
  fi
  rm -rf "$im_dir"
else
  printf 'SKIP: python3 unavailable; skipping --inbox-manifest behavioral test\n' >&2
fi

# === Verify-before-destroy: data-key preflight ===============================
# An off-host set with no data-key archive must abort before any database change.

# V1a. resolve_secrets_archive (the shared detector used by the preflight AND STEP 8)
#      genuinely finds the secrets archive when present and returns non-zero when absent.
rsa_fn="$(sed -n '/^resolve_secrets_archive()/,/^}/p' "$RESTORE_SCRIPT")"
run_rsa() {
  # $1 = "with"|"without" secrets; echoes "FOUND"/"MISSING".
  local d; d="$(mktemp -d)"
  : > "${d}/jarvis_20260701_030000.sql.gz"
  : > "${d}/litellm_20260701_030000.sql.gz"
  [ "$1" = "with" ] && : > "${d}/secrets_20260701_030000.tar.gz.enc"
  ARCHIVE_DIR="$d" TIMESTAMP="20260701_030000" bash -c '
    set -euo pipefail
    '"$rsa_fn"'
    if resolve_secrets_archive >/dev/null; then echo FOUND; else echo MISSING; fi
  ' 2>/dev/null
  rm -rf "$d"
}
if [ -n "$rsa_fn" ] && [ "$(run_rsa with)" = "FOUND" ] && [ "$(run_rsa without)" = "MISSING" ]; then
  pass "resolve_secrets_archive detects a present secrets archive and reports its absence"
else
  printf 'FAIL: resolve_secrets_archive wrong (with=%s without=%s)\n' \
    "$(run_rsa with)" "$(run_rsa without)" >&2
  fail=1
fi

# V1b. The STEP-2.5 data-key preflight runs before the destructive swap, is
# inbox-scoped when the archive is absent, and fails without claiming manual work.
secgate_line="$(line_of '^SECRETS_ARCHIVE="\$\(resolve_secrets_archive' || true)"
if [ -n "$secgate_line" ] && [ -n "$drop_call_line" ] && [ "$secgate_line" -lt "$drop_call_line" ]; then
  pass "the data-key preflight runs before the destructive swap"
else
  printf 'FAIL: data-key preflight (%s) does not precede the destructive swap (%s)\n' \
    "$secgate_line" "$drop_call_line" >&2
  fail=1
fi
sec_block="$(sed -n '/=== STEP 2.5:/,/=== STEP 3:/p' "$RESTORE_SCRIPT")"
if printf '%s' "$sec_block" | grep -q 'if \[ "\$SOURCE" = "inbox" \]' \
   && printf '%s' "$sec_block" | grep -q 'resolve_secrets_archive' \
   && ! printf '%s' "$sec_block" | grep -q 'MANUAL_STEPS_REQUIRED=1' \
   && printf '%s' "$sec_block" | grep -q 'fail_before_destruction'; then
  pass "the data-key preflight is inbox-scoped and fails before destruction without claiming manual steps"
else
  printf 'FAIL: the STEP-2.5 data-key preflight is missing its inbox guard / fail-before-destruction, or wrongly claims manual steps on a path that changed nothing\n' >&2
  fail=1
fi

# === Authenticated inventory binding =========================================
PARSE_INV_FN="$(sed -n '/^parse_authenticated_manifest()/,/^}/p' "$RESTORE_SCRIPT")"
VERIFY_INV_FN="$(sed -n '/^verify_manifest_inventory()/,/^}/p' "$RESTORE_SCRIPT")"
STAGE_INV_FN="$(sed -n '/^stage_manifest_inventory()/,/^}/p' "$RESTORE_SCRIPT")"
inv_dir="$(mktemp -d)"; inv_ts="20260715_120000"
inv_run="0123456789abcdef0123456789abcdef"
inv_manifest="${inv_dir}/manifest_${inv_ts}.json"
printf 'ORIGINAL-J' > "${inv_dir}/jarvis_${inv_ts}.sql.gz"
printf 'ORIGINAL-L' > "${inv_dir}/litellm_${inv_ts}.sql.gz"
printf 'ORIGINAL-P' > "${inv_dir}/pdfs_${inv_ts}.tar.gz"
j_sha="$(sha256sum "${inv_dir}/jarvis_${inv_ts}.sql.gz" | cut -d' ' -f1)"
l_sha="$(sha256sum "${inv_dir}/litellm_${inv_ts}.sql.gz" | cut -d' ' -f1)"
p_sha="$(sha256sum "${inv_dir}/pdfs_${inv_ts}.tar.gz" | cut -d' ' -f1)"
j_size="$(stat -c%s "${inv_dir}/jarvis_${inv_ts}.sql.gz")"
l_size="$(stat -c%s "${inv_dir}/litellm_${inv_ts}.sql.gz")"
p_size="$(stat -c%s "${inv_dir}/pdfs_${inv_ts}.tar.gz")"
write_inv_manifest() {
  printf '{"timestamp":"%s","run_id":"%s","archives":%s}' "$1" "$2" "$3" \
    > "$inv_manifest"
}
good_archives='[{"filename":"jarvis_'"${inv_ts}"'.sql.gz","sha256":"'"${j_sha}"'","size_bytes":'"${j_size}"'},{"filename":"litellm_'"${inv_ts}"'.sql.gz","sha256":"'"${l_sha}"'","size_bytes":'"${l_size}"'},{"filename":"pdfs_'"${inv_ts}"'.tar.gz","sha256":"'"${p_sha}"'","size_bytes":'"${p_size}"'}]'
write_inv_manifest "$inv_ts" "$inv_run" "$good_archives"
inv_file="${inv_dir}/inventory.tsv"; parse_rc=0
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "$inv_file" 2>/dev/null || parse_rc=$?
if [ "$parse_rc" -eq 0 ] && [ "$(wc -l < "$inv_file")" -eq 3 ]; then
  pass "current unencrypted manifest requires databases and PDFs but may omit data keys"
else
  printf 'FAIL: authenticated manifest parser missing/wrong (rc=%s)\n' "$parse_rc" >&2; fail=1
fi

bad_parse_count=0
write_inv_manifest "20260715_120001" "$inv_run" "$good_archives"
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "${inv_dir}/bad.tsv" 2>/dev/null || bad_parse_count=$((bad_parse_count + 1))
dup_archives='[{"filename":"jarvis_'"${inv_ts}"'.sql.gz","sha256":"'"${j_sha}"'","size_bytes":'"${j_size}"'},{"filename":"jarvis_'"${inv_ts}"'.sql.gz","sha256":"'"${j_sha}"'","size_bytes":'"${j_size}"'},{"filename":"litellm_'"${inv_ts}"'.sql.gz","sha256":"'"${l_sha}"'","size_bytes":'"${l_size}"'},{"filename":"pdfs_'"${inv_ts}"'.tar.gz","sha256":"'"${p_sha}"'","size_bytes":'"${p_size}"'}]'
write_inv_manifest "$inv_ts" "$inv_run" "$dup_archives"
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "${inv_dir}/bad.tsv" 2>/dev/null || bad_parse_count=$((bad_parse_count + 1))
write_inv_manifest "$inv_ts" bad-run-id "$good_archives"
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "${inv_dir}/bad.tsv" 2>/dev/null || bad_parse_count=$((bad_parse_count + 1))
path_archives='[{"filename":"../jarvis_'"${inv_ts}"'.sql.gz","sha256":"'"${j_sha}"'","size_bytes":'"${j_size}"'},{"filename":"litellm_'"${inv_ts}"'.sql.gz","sha256":"'"${l_sha}"'","size_bytes":'"${l_size}"'},{"filename":"pdfs_'"${inv_ts}"'.tar.gz","sha256":"'"${p_sha}"'","size_bytes":'"${p_size}"'}]'
write_inv_manifest "$inv_ts" "$inv_run" "$path_archives"
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "${inv_dir}/bad.tsv" 2>/dev/null || bad_parse_count=$((bad_parse_count + 1))
missing_pdf_archives='[{"filename":"jarvis_'"${inv_ts}"'.sql.gz","sha256":"'"${j_sha}"'","size_bytes":'"${j_size}"'},{"filename":"litellm_'"${inv_ts}"'.sql.gz","sha256":"'"${l_sha}"'","size_bytes":'"${l_size}"'}]'
write_inv_manifest "$inv_ts" "$inv_run" "$missing_pdf_archives"
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "${inv_dir}/bad.tsv" 2>/dev/null || bad_parse_count=$((bad_parse_count + 1))
encrypted_without_secrets='[{"filename":"jarvis_'"${inv_ts}"'.sql.gz.enc","sha256":"'"${j_sha}"'","size_bytes":'"${j_size}"'},{"filename":"litellm_'"${inv_ts}"'.sql.gz.enc","sha256":"'"${l_sha}"'","size_bytes":'"${l_size}"'},{"filename":"pdfs_'"${inv_ts}"'.tar.gz.enc","sha256":"'"${p_sha}"'","size_bytes":'"${p_size}"'}]'
write_inv_manifest "$inv_ts" "$inv_run" "$encrypted_without_secrets"
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "${inv_dir}/bad.tsv" 2>/dev/null || bad_parse_count=$((bad_parse_count + 1))
if [ "$bad_parse_count" -eq 6 ]; then
  pass "current manifests reject substitution, duplicate roles, traversal, and missing required roles"
else
  printf 'FAIL: authenticated manifest accepted an invalid binding\n' >&2; fail=1
fi

encrypted_archives='[{"filename":"jarvis_'"${inv_ts}"'.sql.gz.enc","sha256":"'"${j_sha}"'","size_bytes":'"${j_size}"'},{"filename":"litellm_'"${inv_ts}"'.sql.gz.enc","sha256":"'"${l_sha}"'","size_bytes":'"${l_size}"'},{"filename":"pdfs_'"${inv_ts}"'.tar.gz.enc","sha256":"'"${p_sha}"'","size_bytes":'"${p_size}"'},{"filename":"secrets_'"${inv_ts}"'.tar.gz.enc","sha256":"'"${p_sha}"'","size_bytes":'"${p_size}"'}]'
write_inv_manifest "$inv_ts" "$inv_run" "$encrypted_archives"
encrypted_parse_rc=0
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "${inv_dir}/encrypted.tsv" 2>/dev/null || encrypted_parse_rc=$?
if [ "$encrypted_parse_rc" -eq 0 ] && [ "$(wc -l < "${inv_dir}/encrypted.tsv")" -eq 4 ]; then
  pass "current encrypted manifests require the exact data-key archive role"
else
  printf 'FAIL: current encrypted manifest with all mandatory roles was rejected\n' >&2
  fail=1
fi

write_inv_manifest "$inv_ts" "$inv_run" "$good_archives"
bash -c 'set -euo pipefail; '"$PARSE_INV_FN"'; parse_authenticated_manifest "$1" "$2" "$3"' \
  _ "$inv_manifest" "$inv_ts" "$inv_file" 2>/dev/null || true
printf EXTRA > "${inv_dir}/qdrant_extra_${inv_ts}.snapshot"
extra_rc=0
bash -c 'set -euo pipefail; '"$VERIFY_INV_FN"'; verify_manifest_inventory "$1" "$2" "$3"' \
  _ "$inv_dir" "$inv_ts" "$inv_file" 2>/dev/null || extra_rc=$?
rm -f "${inv_dir}/qdrant_extra_${inv_ts}.snapshot"
stage_dir="${inv_dir}/private"; mkdir -m 700 "$stage_dir"; stage_rc=0
bash -c 'set -euo pipefail; '"$VERIFY_INV_FN"$'\n'"$STAGE_INV_FN"'; stage_manifest_inventory "$1" "$2" "$3" "$4"' \
  _ "$inv_dir" "$inv_ts" "$inv_file" "$stage_dir" 2>/dev/null || stage_rc=$?
printf 'SWAPPED-J' > "${inv_dir}/jarvis_${inv_ts}.sql.gz"
staged_rc=0
bash -c 'set -euo pipefail; '"$VERIFY_INV_FN"'; verify_manifest_inventory "$1" "$2" "$3"' \
  _ "$stage_dir" "$inv_ts" "$inv_file" 2>/dev/null || staged_rc=$?
if [ "$extra_rc" -ne 0 ] && [ "$stage_rc" -eq 0 ] && [ "$staged_rc" -eq 0 ] \
   && [ "$(cat "${stage_dir}/jarvis_${inv_ts}.sql.gz" 2>/dev/null || true)" = ORIGINAL-J ] \
   && [ "$(cat "${stage_dir}/pdfs_${inv_ts}.tar.gz" 2>/dev/null || true)" = ORIGINAL-P ]; then
  pass "inventory rejects extras and private staging defeats a post-check path swap"
else
  printf 'FAIL: exact inventory/staging wrong (extra=%s stage=%s staged=%s)\n' \
    "$extra_rc" "$stage_rc" "$staged_rc" >&2; fail=1
fi
rm -rf "$inv_dir"

# === Verify-before-destroy: exact safety pre-backup ====================
# Correlate the rollback point by a caller-assigned run ID and its authenticated,
# complete archive inventory. A successful check must copy the exact signed set
# into a mode-700 directory on the durable backup volume before any DROP.
run_sbf() {
  # $1 = backup rc, $2 = expected run id, $3 = last-run run id, $4 = fixture mode.
  local d key ts="20260715_120000" manifest derived out
  d="$(mktemp -d)"; key="${d}/backup.key"; manifest="${d}/manifest_${ts}.json"
  printf 'fixture-backup-key\n' > "$key"
  printf 'JARVISDBDATA' > "${d}/jarvis_${ts}.sql.gz.enc"
  printf 'LITELLMDBDATA' > "${d}/litellm_${ts}.sql.gz.enc"
  printf 'PDFDATA' > "${d}/pdfs_${ts}.tar.gz.enc"
  printf 'SECRETSDATA' > "${d}/secrets_${ts}.tar.gz.enc"
  local jsha lsha psha ssha jsz lsz psz ssz entries
  jsha="$(sha256sum "${d}/jarvis_${ts}.sql.gz.enc" | cut -d' ' -f1)"
  lsha="$(sha256sum "${d}/litellm_${ts}.sql.gz.enc" | cut -d' ' -f1)"
  psha="$(sha256sum "${d}/pdfs_${ts}.tar.gz.enc" | cut -d' ' -f1)"
  ssha="$(sha256sum "${d}/secrets_${ts}.tar.gz.enc" | cut -d' ' -f1)"
  jsz="$(stat -c%s "${d}/jarvis_${ts}.sql.gz.enc")"
  lsz="$(stat -c%s "${d}/litellm_${ts}.sql.gz.enc")"
  psz="$(stat -c%s "${d}/pdfs_${ts}.tar.gz.enc")"
  ssz="$(stat -c%s "${d}/secrets_${ts}.tar.gz.enc")"
  entries='[{"filename":"jarvis_'"${ts}"'.sql.gz.enc","sha256":"'"${jsha}"'","size_bytes":'"${jsz}"'},{"filename":"litellm_'"${ts}"'.sql.gz.enc","sha256":"'"${lsha}"'","size_bytes":'"${lsz}"'},{"filename":"pdfs_'"${ts}"'.tar.gz.enc","sha256":"'"${psha}"'","size_bytes":'"${psz}"'},{"filename":"secrets_'"${ts}"'.tar.gz.enc","sha256":"'"${ssha}"'","size_bytes":'"${ssz}"'}]'
  case "$4" in
    empty) entries='[]' ;;
    missing_secrets)
      entries='[{"filename":"jarvis_'"${ts}"'.sql.gz.enc","sha256":"'"${jsha}"'","size_bytes":'"${jsz}"'},{"filename":"litellm_'"${ts}"'.sql.gz.enc","sha256":"'"${lsha}"'","size_bytes":'"${lsz}"'},{"filename":"pdfs_'"${ts}"'.tar.gz.enc","sha256":"'"${psha}"'","size_bytes":'"${psz}"'}]'
      rm -f "${d}/secrets_${ts}.tar.gz.enc" ;;
    missing_pdfs)
      entries='[{"filename":"jarvis_'"${ts}"'.sql.gz.enc","sha256":"'"${jsha}"'","size_bytes":'"${jsz}"'},{"filename":"litellm_'"${ts}"'.sql.gz.enc","sha256":"'"${lsha}"'","size_bytes":'"${lsz}"'},{"filename":"secrets_'"${ts}"'.tar.gz.enc","sha256":"'"${ssha}"'","size_bytes":'"${ssz}"'}]'
      rm -f "${d}/pdfs_${ts}.tar.gz.enc" ;;
    duplicate)
      entries='[{"filename":"jarvis_'"${ts}"'.sql.gz.enc","sha256":"'"${jsha}"'","size_bytes":'"${jsz}"'},{"filename":"jarvis_'"${ts}"'.sql.gz.enc","sha256":"'"${jsha}"'","size_bytes":'"${jsz}"'},{"filename":"litellm_'"${ts}"'.sql.gz.enc","sha256":"'"${lsha}"'","size_bytes":'"${lsz}"'},{"filename":"pdfs_'"${ts}"'.tar.gz.enc","sha256":"'"${psha}"'","size_bytes":'"${psz}"'},{"filename":"secrets_'"${ts}"'.tar.gz.enc","sha256":"'"${ssha}"'","size_bytes":'"${ssz}"'}]' ;;
  esac
  printf '{"timestamp":"%s","run_id":"%s","app_version":"1.2.0","schema_version":200,"created_at":"2026-07-15T12:00:00+00:00","archives":%s}' \
    "$ts" "$2" "$entries" > "$manifest"
  if [ "$4" != "unsigned" ]; then
    derived="$(openssl dgst -sha256 -hmac 'jarvis-manifest-v1' -r < "$key" | cut -d' ' -f1)"
    openssl dgst -sha256 -mac HMAC -macopt "hexkey:${derived}" -r < "$manifest" \
      | cut -d' ' -f1 > "${manifest}.hmac"
  fi
  [ "$4" = "swapped" ] && printf 'SWAPPED' > "${d}/jarvis_${ts}.sql.gz.enc"
  printf '{"attempted_at":"2026-07-15T12:00:00+00:00","timestamp":"%s","run_id":"%s","succeeded":true}' \
    "$ts" "$3" > "${d}/.last_run.json"
  out="$(BACKUP_DIR="$d" BACKUP_TRIGGER_DIR="${d}/trigger" \
    RESTORE_INBOX_DIR="${d}/inbox" HOST_SECRETS_DIR="${d}/host-secrets" \
    BACKUP_ENCRYPT_KEYFILE="$key" bash -c '
      set -euo pipefail
      source "$1" --functions-only
      if safety_backup_is_fresh "$2" "$3"; then
        staged="${SAFETY_STAGING_DIR:-}"
        if [ -d "$staged" ]; then
          printf "FRESH|%s|%s|%s" "$(stat -c %a "$staged")" \
            "$(cat "$staged/jarvis_20260715_120000.sql.gz.enc")" \
            "$(find "$staged" -maxdepth 1 -type f | wc -l)"
        else
          printf "FRESH|NO-STAGE"
        fi
      else
        printf STALE
      fi
    ' _ "$RESTORE_SCRIPT" "$1" "$2" 2>/dev/null || true)"
  rm -rf "$d"
  printf '%s' "$out"
}
expected_run="0123456789abcdef0123456789abcdef"
other_run="fedcba9876543210fedcba9876543210"
sbf_fresh="$(run_sbf 0 "$expected_run" "$expected_run" good)"
sbf_mismatch="$(run_sbf 0 "$expected_run" "$other_run" good)"
sbf_rcfail="$(run_sbf 1 "$expected_run" "$expected_run" good)"
sbf_empty="$(run_sbf 0 "$expected_run" "$expected_run" empty)"
sbf_missing="$(run_sbf 0 "$expected_run" "$expected_run" missing_secrets)"
sbf_missing_pdfs="$(run_sbf 0 "$expected_run" "$expected_run" missing_pdfs)"
sbf_duplicate="$(run_sbf 0 "$expected_run" "$expected_run" duplicate)"
sbf_swapped="$(run_sbf 0 "$expected_run" "$expected_run" swapped)"
sbf_unsigned="$(run_sbf 0 "$expected_run" "$expected_run" unsigned)"
if [ "$sbf_fresh" = "FRESH|700|JARVISDBDATA|7" ] \
   && [ "$sbf_mismatch" = "STALE" ] && [ "$sbf_rcfail" = "STALE" ] \
   && [ "$sbf_empty" = "STALE" ] && [ "$sbf_missing" = "STALE" ] \
   && [ "$sbf_missing_pdfs" = "STALE" ] \
   && [ "$sbf_duplicate" = "STALE" ] && [ "$sbf_swapped" = "STALE" ] \
   && [ "$sbf_unsigned" = "STALE" ]; then
  pass "safety backup requires an authenticated exact inventory and stages immutable recovery bytes"
else
  printf 'FAIL: safety backup contract wrong (fresh=%s mismatch=%s rcfail=%s empty=%s missing-secrets=%s missing-pdfs=%s duplicate=%s swapped=%s unsigned=%s)\n' \
    "$sbf_fresh" "$sbf_mismatch" "$sbf_rcfail" "$sbf_empty" "$sbf_missing" \
    "$sbf_missing_pdfs" "$sbf_duplicate" "$sbf_swapped" "$sbf_unsigned" >&2
  fail=1
fi
check "post-DROP cleanup preserves and reports the staged safety recovery directory" \
  'Recovery copy: .*SAFETY_STAGING_DIR'
# STEP 4 must capture backup.sh's exit code (not merely WARN) and gate on freshness,
# failing before destruction when it is stale/failed. It must NOT claim manual steps
# are required: nothing has been changed yet on that path, and the EXIT trap sets the
# flag for real post-destruction failures.
step4_block="$(sed -n '/=== STEP 4:/,/=== STEP 4.5:/p' "$RESTORE_SCRIPT")"
if printf '%s' "$step4_block" | grep -q 'SAFETY_RC=' \
   && printf '%s' "$step4_block" | grep -q 'safety_backup_is_fresh "\$SAFETY_RC" "\$SAFETY_RUN_ID"' \
   && ! printf '%s' "$step4_block" | grep -q 'MANUAL_STEPS_REQUIRED=1' \
   && printf '%s' "$step4_block" | grep -q 'fail_before_destruction'; then
  pass "STEP 4 captures backup.sh's rc, gates on freshness, and fails before destruction without claiming manual steps"
else
  printf 'FAIL: STEP 4 does not capture the backup rc / gate on freshness / fail before destruction, or wrongly claims manual steps on a path that changed nothing\n' >&2
  fail=1
fi

# V3. Honest status: any terminal FAILURE inside the destructive window (DROP_STARTED=1)
#     sets manual_steps_required in the single EXIT-trap status writer.
check "post-DROP failures set manual_steps_required in the EXIT trap" \
  '\[ "\$STATE" = "failed" \] && \[ "\$DROP_STARTED" = "1" \]; then MANUAL_STEPS_REQUIRED=1'

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

# === Signed-restore ratchet: two locations, never taken back =================
# The requirement may only ever be ADDED. Either copy arms it, so no lifecycle
# state — a replaced checkout, a cleaned host state directory, a resumed update —
# may move a restore back from "signature required" to "not required".
RATCHET_FN="$(sed -n '/^manifest_signature_required()/,/^}/p' "$RESTORE_SCRIPT")"
PUBLISH_FN="$(sed -n '/^publish_manifest_signature()/,/^}/p' "$BACKUP_SCRIPT")"

# ratchet_answer OLD_DIR DURABLE_DIR -> "required" | "not-required"
ratchet_answer() {
  SOURCE=local HOST_SECRETS_DIR="$1" BACKUP_STATE_DIR="$2" bash -c '
    set -euo pipefail
    MANIFEST_HMAC_MARKER="${HOST_SECRETS_DIR}/manifest-hmac-required"
    MANIFEST_HMAC_MARKER_DURABLE="${BACKUP_STATE_DIR}/manifest-hmac-required"
    '"$RATCHET_FN"'
    if manifest_signature_required; then echo required; else echo not-required; fi
  '
}
rt_root="$(mktemp -d)"
mkdir -p "${rt_root}"/{old,new,empty-old,empty-new}
: > "${rt_root}/old/manifest-hmac-required"
: > "${rt_root}/new/manifest-hmac-required"
ratchet_case() {  # ratchet_case <description> <old-dir> <durable-dir> <want>
  local got; got="$(ratchet_answer "$2" "$3")"
  if [ "$got" = "$4" ]; then pass "$1"; else
    printf 'FAIL: %s (got=%s want=%s)\n' "$1" "$got" "$4" >&2; fail=1; fi
}
ratchet_case "ratchet: the pre-relocation copy alone still requires a signature" \
  "${rt_root}/old" "${rt_root}/empty-new" required
ratchet_case "ratchet: the durable copy alone requires a signature" \
  "${rt_root}/empty-old" "${rt_root}/new" required
ratchet_case "ratchet: both copies require a signature" \
  "${rt_root}/old" "${rt_root}/new" required
ratchet_case "ratchet: a fresh install with neither copy does not require one" \
  "${rt_root}/empty-old" "${rt_root}/empty-new" not-required

# Lifecycle: a run that publishes a signature on an install armed by an earlier
# release must leave BOTH copies present. Nothing removes either one.
lc_root="$(mktemp -d)"; lc_ts="20260801_090000"
mkdir -p "${lc_root}/old" "${lc_root}/new" "${lc_root}/backups"
: > "${lc_root}/old/manifest-hmac-required"
printf '{}' > "${lc_root}/backups/manifest_${lc_ts}.json"
lc_rc=0
BACKUP_DIR="${lc_root}/backups" TIMESTAMP="$lc_ts" ENCRYPT=1 \
HOST_SECRETS_DIR="${lc_root}/old" MANIFEST_HMAC_MARKER="${lc_root}/old/manifest-hmac-required" \
BACKUP_STATE_DIR="${lc_root}/new" MANIFEST_HMAC_MARKER_DURABLE="${lc_root}/new/manifest-hmac-required" \
bash -c '
  set -uo pipefail
  sign_manifest() { printf "signed\n" > "${1}.hmac"; }
  '"$PUBLISH_FN"'
  publish_manifest_signature
' 2>/dev/null || lc_rc=$?
if [ "$lc_rc" -eq 0 ] \
   && [ -e "${lc_root}/old/manifest-hmac-required" ] \
   && [ -e "${lc_root}/new/manifest-hmac-required" ] \
   && [ "$(ratchet_answer "${lc_root}/old" "${lc_root}/new")" = required ]; then
  pass "ratchet: publishing a signature leaves both marker copies in place"
else
  printf 'FAIL: publishing a signature did not leave both copies (rc=%s old=%s durable=%s)\n' \
    "$lc_rc" "$([ -e "${lc_root}/old/manifest-hmac-required" ] && echo yes || echo no)" \
    "$([ -e "${lc_root}/new/manifest-hmac-required" ] && echo yes || echo no)" >&2
  fail=1
fi
# A state cleaner, a changed HOME, or a differing XDG_STATE_HOME can take the
# durable copy away. The requirement must survive on the pre-relocation copy.
rm -f "${lc_root}/new/manifest-hmac-required"
ratchet_case "ratchet: losing only the durable copy still requires a signature" \
  "${lc_root}/old" "${lc_root}/new" required
rm -rf "$rt_root" "$lc_root"

# === Break-glass is same-host only ===========================================
# The override exists for the disaster where the sole SAME-HOST set predates
# signing. An off-host set has nothing on the fresh host to check it against, so
# it is refused outright — and terminal access must not take that refusal back.
GLASS_FN="$(sed -n '/^break_glass_accepted()/,/^}/p' "$RESTORE_SCRIPT")"
glass_first="$(printf '%s\n' "$GLASS_FN" | sed -n '2,$p' \
  | grep -vE '^[[:space:]]*(#|$)' | head -1 | sed -E 's/^[[:space:]]+//')"
if [ "$glass_first" = '[ "$SOURCE" != "inbox" ] || return 1' ]; then
  pass "break-glass refuses an off-host source as its very first check"
else
  printf 'FAIL: break_glass_accepted does not open with the off-host refusal (first statement: %s)\n' \
    "$glass_first" >&2
  fail=1
fi

# Behavioural proof. The gate also requires a real terminal, so the two cases run
# under a pseudo-terminal with the acceptance phrase typed: identical input, and
# only the source differs.
if command -v script >/dev/null 2>&1; then
  glass_harness="$(mktemp)"
  {
    printf 'set -uo pipefail\n'
    printf 'BREAK_GLASS_PHRASE="I-ACCEPT-UNVERIFIED-BACKUP"\n'
    printf '%s\n' "$GLASS_FN"
    printf 'if break_glass_accepted; then printf "GLASS-ACCEPTED\\n"; else printf "GLASS-REFUSED\\n"; fi\n'
  } > "$glass_harness"
  glass_answer() {  # glass_answer <source> -> GLASS-ACCEPTED | GLASS-REFUSED
    printf 'I-ACCEPT-UNVERIFIED-BACKUP\n' \
      | SOURCE="$1" JARVIS_RESTORE_ALLOW_LEGACY=1 \
        script -qec "bash ${glass_harness}" /dev/null 2>/dev/null \
      | tr -d '\r' | grep -oE 'GLASS-(ACCEPTED|REFUSED)' | tail -1
  }
  glass_local="$(glass_answer local)"
  glass_inbox="$(glass_answer inbox)"
  if [ "$glass_local" = GLASS-ACCEPTED ] && [ "$glass_inbox" = GLASS-REFUSED ]; then
    pass "break-glass accepts a typed same-host override and still refuses an off-host one"
  else
    printf 'FAIL: break-glass source gate (local=%s inbox=%s; both should differ)\n' \
      "$glass_local" "$glass_inbox" >&2
    fail=1
  fi
  rm -f "$glass_harness"
else
  printf 'SKIP: `script` is unavailable, so the break-glass terminal gate cannot be driven\n' >&2
fi

# === Compose wiring ==========================================================

cmp_check() {
  if grep -Eq "$2" "$COMPOSE"; then pass "$1"; else
    printf 'FAIL: %s (pattern: %s)\n' "$1" "$2" >&2; fail=1; fi
}
cmp_check "sidecar mounts restore.sh" 'restore\.sh:/usr/local/bin/restore\.sh:ro'
cmp_check "sidecar mounts the durable state dir, falling back to ./secrets when unrecorded" \
  '\$\{JARVIS_STATE_DIR:-\./secrets\}:/backup-state:rw'
cmp_check "sidecar env stamps JARVIS_VERSION (manifest app_version)" 'JARVIS_VERSION: \$\{JARVIS_VERSION'
cmp_check "entrypoint runs restore.sh on a restore request" \
  'restore_request\.json.*restore\.sh|if \[ -f /backup-trigger/\.restore_request\.json'
cmp_check "named volume restore_staging is declared" '^  restore_staging:'
cmp_check "sidecar mounts the migrations dir (ro) for the compat code-max read" 'db/migrations:/app/db/migrations:ro'
cmp_check "sidecar mounts postgres_data (ro) so the disk preflight can size free space" \
  'postgres_data:/postgres-data:ro'
cmp_check "entrypoint reconciles private stranded swap state on startup" \
  '/backups/\.lifecycle/restore-swap-state\.json.*restore\.sh --recover'
cmp_check "entrypoint refreshes the inbox manifest each loop (restore.sh --inbox-manifest)" \
  'restore\.sh --inbox-manifest'

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
