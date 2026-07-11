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
consume_line="$(line_of '^rm -f "\$REQUEST_FILE"')"
if [ -n "$consume_line" ] && [ -n "$drop_call_line" ] && [ "$consume_line" -lt "$drop_call_line" ]; then
  pass "request sentinel is rm -f'd BEFORE the destructive restore_one_db_swap call"
else
  printf 'FAIL: request consume (%s) is not before the restore_one_db_swap call (%s)\n' \
    "$consume_line" "$drop_call_line" >&2
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

# S2. Swap-state file: written BEFORE each of the four transitions and read by
#     --recover to know which db to reconcile.
check "records the swap state in .restore_swap_state.json" '\.restore_swap_state\.json'
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
check "--recover marks RECOVER_MODE so the EXIT trap skips the request re-consume" \
  'RECOVER_MODE=1'
check "the EXIT trap skips the request re-consume in --recover mode" \
  '\[ "\$RECOVER_MODE" = "1" \] \|\| rm -f "\$REQUEST_FILE"'
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

# S6. The safety pre-backup is forced past the (4.3) maintenance skip-guard: the
#     restore's own .maintenance is already up, so the backup must be told to run.
check "safety pre-backup is forced past the maintenance skip-guard" 'export BACKUP_FORCE=1'

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
check "the watchdog drops a .restore_timeout marker on deadline" \
  ': > "\$\{TRIGGER_DIR\}/\.restore_timeout"'
check "the watchdog signals the main process on deadline" 'kill "\$MAIN_PID"'
check "routes SIGTERM into the single EXIT trap" "trap 'exit 143' TERM"
check "routes SIGINT into the single EXIT trap" "trap 'exit 130' INT"
check "_cleanup words a timeout distinctly from a mid-reload failure" \
  'restore exceeded its time limit'
check "clears a stale .restore_timeout marker at the start of a run" \
  'rm -f "\$\{TRIGGER_DIR\}/\.restore_timeout"'

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
# lift .maintenance + drop .restore_timeout; with it PRESENT it must HOLD
# .maintenance (never re-expose a destroyed DB).
wd_block="$(sed -n '/: >/,/^      fi/p' "$RESTORE_SCRIPT")"
run_wd() {
  # $1 = "destroyed" -> pre-create .destructive; anything else -> absent.
  local d; d="$(mktemp -d)"
  touch "${d}/.maintenance"
  [ "$1" = "destroyed" ] && touch "${d}/.destructive"
  TRIGGER_DIR="$d" MAINTENANCE_SENTINEL="${d}/.maintenance" \
  MAINTENANCE_DESTRUCTIVE="${d}/.destructive" bash -c '
    set -euo pipefail
    '"$wd_block"'
  ' 2>/dev/null
  local maint=absent timeout=absent
  [ -f "${d}/.maintenance" ] && maint=present
  [ -f "${d}/.restore_timeout" ] && timeout=present
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

# 8. exit 0 after a recorded terminal failure: every non-zero exit in the file is
#    a perl statement (semicolon-terminated) inside qdrant_http_body — there is NO
#    bash-level non-zero exit that could crash-restart the sidecar.
check "fails before destruction with exit 0" 'fail_before_destruction\(\)'
check "fails during/after the drop with exit 0" 'step5_fail\(\)'
nonzero_all="$(grep -Ec 'exit[[:space:]]+[1-9]' "$RESTORE_SCRIPT" || true)"
# Whitelisted non-zero exits: the perl (semicolon-terminated) exits inside
# qdrant_http_body, and the TERM/INT trap handlers (exit 143/130 route a signal
# into the single EXIT trap, which then forces exit 0 — a real script exit is
# still always 0, so the sidecar never crash-restarts).
nonzero_ok="$(grep -Ec "exit[[:space:]]+[1-9];|trap 'exit [0-9]+'" "$RESTORE_SCRIPT" || true)"
if [ "$nonzero_all" -eq "$nonzero_ok" ]; then
  pass "no unguarded bash-level non-zero exit (terminal failures exit 0; sidecar never crash-restarts)"
else
  printf 'FAIL: an unguarded bash-level non-zero exit exists (all=%s ok=%s)\n' "$nonzero_all" "$nonzero_ok" >&2
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

# I8b. The operator-supplied secrets archive is untrusted: reject symlink/hardlink
#      members before extraction (a symlink could redirect a write into the now-rw
#      /host-secrets mount), in addition to the absolute/'..' path check.
check "rejects symlink/hardlink members in the secrets archive" \
  'contains a symlink or hardlink member'

# I9. A clean restore now lifts maintenance regardless of source: the inbox path
#     materializes the restored ./secrets into HOST_SECRETS_DIR and writes the
#     rotation marker, so the app containers self-restart onto the rebound role —
#     no operator step, no hold. The lift gate no longer excludes inbox.
check "lifts maintenance on any clean restore (inbox self-recovers)" \
  '\[ "\$RESTORE_CLEAN" = "1" \] \|\| \[ "\$DROP_STARTED" = "0" \]'
if grep -Eq '!= "inbox"' "$RESTORE_SCRIPT"; then
  printf 'FAIL: the lift gate still excludes inbox (the self-restart contract removed that hold)\n' >&2
  fail=1
else
  pass "the lift gate no longer excludes inbox (old operator hold removed)"
fi

# I9b. Zero-touch off-host recovery: STEP 8 materializes the restored secrets into
#      HOST_SECRETS_DIR and writes the .secrets_rotated marker that drives each
#      postgres-connecting service's self-restart, replacing the old "recreate the
#      containers / clear .destructive" operator step.
check "inbox restore materializes the restored secrets into the host secrets dir" \
  'cp -- "\$sfile" "\$\{HOST_SECRETS_DIR\}'
check "inbox restore writes the .secrets_rotated marker for self-restart" \
  'mv -f "\$\{TRIGGER_DIR\}/\.secrets_rotated\.tmp" "\$\{TRIGGER_DIR\}/\.secrets_rotated"'
check "the STEP-9 inbox echo reports automatic self-restart (no manual recreate)" \
  'off-host restore complete.*self-restarting'

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

# I13. --inbox-manifest is a READ-ONLY inventory pass: it writes a SANITIZED manifest
#      (names/booleans only) the app's GET /inbox lists, and MANIFEST_MODE short-circuits
#      the EXIT trap so it never consumes the restore request, writes a status, or shreds
#      the operator key. Static structure + a behavioral run against a seeded fake inbox.
check "defines the --inbox-manifest inventory branch" '\[ "\$\{1:-\}" = "--inbox-manifest" \]'
check "the inbox-manifest branch sets MANIFEST_MODE" 'MANIFEST_MODE=1'
check "the EXIT trap short-circuits in MANIFEST_MODE (no consume/status/shred)" \
  '\[ "\$MANIFEST_MODE" = "1" \] && exit 0'
check "the manifest emits only names/booleans (no path/key fields)" \
  '"timestamp":"%s","complete":%s,"has_secrets":%s,"has_key":%s'

if command -v python3 >/dev/null 2>&1; then
  im_dir="$(mktemp -d)"
  im_inbox="${im_dir}/inbox"
  im_trig="${im_dir}/trig"
  mkdir -p "$im_inbox" "$im_trig"
  # complete + secrets + key at ts A; jarvis-only (incomplete) at ts B; junk ignored.
  : > "${im_inbox}/jarvis_20260701_030000.sql.gz"
  : > "${im_inbox}/litellm_20260701_030000.sql.gz.enc"
  : > "${im_inbox}/secrets_20260701_030000.tar.gz.enc"
  : > "${im_inbox}/jarvis_20260630_020000.sql.gz"
  : > "${im_inbox}/not-an-archive.txt"
  printf 'SECRETKEYBYTES' > "${im_inbox}/operator_key"
  # A pending restore request the inventory pass must NOT consume.
  printf '{"timestamp":"20260701_030000","confirm":"RESTORE"}' > "${im_trig}/.restore_request.json"
  im_rc=0
  RESTORE_INBOX_DIR="$im_inbox" BACKUP_TRIGGER_DIR="$im_trig" \
    bash "$RESTORE_SCRIPT" --inbox-manifest >/dev/null 2>&1 || im_rc=$?
  if python3 - "${im_trig}/.inbox_manifest.json" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
d = json.loads(raw)
by = {e["timestamp"]: e for e in d}
assert set(by) == {"20260701_030000", "20260630_020000"}, by
assert by["20260701_030000"] == {
    "timestamp": "20260701_030000", "complete": True, "has_secrets": True, "has_key": True}, by
assert by["20260630_020000"]["complete"] is False, by
assert by["20260630_020000"]["has_secrets"] is False, by
# Sanitized: no path or key material anywhere in the JSON.
assert "operator_key" not in raw and "SECRETKEYBYTES" not in raw and "/" not in raw, raw
PY
  then
    pass "--inbox-manifest writes a correct SANITIZED manifest (complete/has_secrets/has_key)"
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
cmp_check "sidecar mounts postgres_data (ro) so the disk preflight can size free space" \
  'postgres_data:/postgres-data:ro'
cmp_check "entrypoint reconciles a stranded swap on startup (restore.sh --recover)" \
  'restore_swap_state\.json.*restore\.sh --recover'
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
