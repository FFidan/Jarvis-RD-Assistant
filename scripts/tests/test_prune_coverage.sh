#!/usr/bin/env bash
# test_prune_coverage.sh — assert the backup-lifecycle DESTRUCTIVE sidecar side is
# correct and fail-safe: scripts/prune.sh (the delete executor), backup.sh's
# keep-last-N retention, restore.sh's inbox-tar sanitization, and the compose loop
# hardening + delete-branch wiring.
#
# prune.sh is exercised as a real subprocess against a temp dir (boundary-adapter
# shape); backup.sh's retention helper + restore.sh's tar-member guard are checked
# both structurally and behaviorally (single-sourced blocks run in isolation, no
# live DB / Qdrant / sidecar).
#
# Run: bash scripts/tests/test_prune_coverage.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRUNE_SCRIPT="${SCRIPT_DIR}/../prune.sh"
BACKUP_SCRIPT="${SCRIPT_DIR}/../backup.sh"
RESTORE_SCRIPT="${SCRIPT_DIR}/../restore.sh"
COMPOSE="${SCRIPT_DIR}/../../docker-compose.yml"

fail=0
pass() { printf 'PASS: %s\n' "$1"; }
checkf() {
  # checkf <file> <human description> <grep -E pattern>
  if grep -Eq "$3" "$1"; then pass "$2"; else
    printf 'FAIL: %s (pattern: %s)\n' "$2" "$3" >&2; fail=1; fi
}
line_of() { grep -nE "$2" "$1" | head -1 | cut -d: -f1; }

for f in "$PRUNE_SCRIPT" "$BACKUP_SCRIPT" "$RESTORE_SCRIPT" "$COMPOSE"; do
  [ -r "$f" ] || { printf 'FAIL: cannot read %s\n' "$f" >&2; exit 1; }
done

# === prune.sh — static structure =============================================
checkf "$PRUNE_SCRIPT" "prune.sh uses the bash shebang" '^#!/usr/bin/env bash'
if [ -x "$PRUNE_SCRIPT" ]; then
  pass "prune.sh is executable (the sidecar execs it directly)"
else
  printf 'FAIL: prune.sh is NOT executable — the sidecar exec would fail with exit 126\n' >&2; fail=1
fi
# -e must NOT be enabled (a benign failure must not abort the run mid-delete).
if grep -Eq '^set -e|^set -[a-z]*e' "$PRUNE_SCRIPT"; then
  printf 'FAIL: prune.sh sets -e (a benign already-gone would abort/crash the loop)\n' >&2; fail=1
else
  pass "prune.sh does NOT set -e (benign failures never abort the run)"
fi
checkf "$PRUNE_SCRIPT" "prune.sh re-validates candidate files (valid_archive_name)" \
  '^valid_archive_name\(\)'
checkf "$PRUNE_SCRIPT" "prune.sh version-gates the request" 'SUPPORTED_VERSION'
checkf "$PRUNE_SCRIPT" "prune.sh requires the DELETE confirm token" '"confirm".*DELETE'
checkf "$PRUNE_SCRIPT" "prune.sh refuses an in-flight restore timestamp" '^restore_in_flight_ts\(\)'
checkf "$PRUNE_SCRIPT" "prune.sh consumes the delete request sentinel" '\.delete_request\.json'

# AT-MOST-ONCE: the request is rm -f'd BEFORE any rm of an archive file.
consume_line="$(line_of "$PRUNE_SCRIPT" 'rm -f "\$REQUEST_FILE"')"
delete_line="$(line_of "$PRUNE_SCRIPT" 'rm -f -- "\$f"')"
if [ -n "$consume_line" ] && [ -n "$delete_line" ] && [ "$consume_line" -lt "$delete_line" ]; then
  pass "prune.sh consumes the request BEFORE deleting any archive (at-most-once)"
else
  printf 'FAIL: request consume (%s) is not before the archive delete (%s)\n' \
    "$consume_line" "$delete_line" >&2; fail=1
fi

# Every terminal path exits 0 (a non-zero exit would crash-restart the sidecar).
if grep -Eq 'exit[[:space:]]+[1-9]' "$PRUNE_SCRIPT"; then
  printf 'FAIL: prune.sh has a non-zero exit (the loop must never crash on a delete)\n' >&2; fail=1
else
  pass "prune.sh never exits non-zero (sidecar never crash-restarts on a delete)"
fi

if bash -n "$PRUNE_SCRIPT"; then pass "bash -n parses prune.sh"; else
  printf 'FAIL: bash -n found a syntax error in prune.sh\n' >&2; fail=1; fi

# === prune.sh — behavioral (real subprocess, temp dir) =======================
# Helper: seed a backups dir with the full archive set of one or more timestamps
# (plus a non-archive decoy that matches the delete glob but must survive).
seed_backups() {
  local dir="$1"; shift
  local ts
  for ts in "$@"; do
    touch "${dir}/jarvis_${ts}.sql.gz" "${dir}/litellm_${ts}.sql.gz.enc" \
          "${dir}/secrets_${ts}.tar.gz" "${dir}/qdrant_kg_entities_${ts}.snapshot" \
          "${dir}/manifest_${ts}.json"
  done
}
run_prune() { BACKUP_TRIGGER_DIR="$1" BACKUP_DIR="$2" bash "$PRUNE_SCRIPT"; }
ts_present() { ls "$2"/*_"$1".* >/dev/null 2>&1; }

# 1) A delete request removes ONLY the named point's archives + manifest; a second
#    point and a glob-matching non-archive decoy both survive; the request is
#    consumed and .last_delete.json records the deletions.
d="$(mktemp -d)"; b="$(mktemp -d)"
seed_backups "$b" 20260101_010101 20260202_020202
touch "${b}/evilscript_20260101_010101.sh"   # matches *_TS.* but is not an archive
printf '{"timestamps": ["20260101_010101"], "confirm": "DELETE", "requested_at": "2026-07-08T10:00:00+00:00", "version": 1}' \
  > "${d}/.delete_request.json"
rc=0; run_prune "$d" "$b" >/dev/null 2>&1 || rc=$?
b1ok=1
[ "$rc" -eq 0 ] || b1ok=0
# target archives gone (check the actual archive files, not the glob — the decoy
# also matches *_TS.*, so a glob check would falsely see the point as present).
for a in jarvis_20260101_010101.sql.gz litellm_20260101_010101.sql.gz.enc \
         secrets_20260101_010101.tar.gz manifest_20260101_010101.json \
         qdrant_kg_entities_20260101_010101.snapshot; do
  [ -f "${b}/${a}" ] && b1ok=0
done
ts_present 20260202_020202 "$b" || b1ok=0            # other point survives
[ -f "${b}/evilscript_20260101_010101.sh" ] || b1ok=0 # decoy survives (re-validated)
[ -f "${d}/.delete_request.json" ] && b1ok=0         # request consumed
[ -f "${d}/.last_delete.json" ] || b1ok=0
grep -q '"jarvis_20260101_010101.sql.gz"' "${d}/.last_delete.json" 2>/dev/null || b1ok=0
if [ "$b1ok" -eq 1 ]; then
  pass "delete request removes ONLY the named point (decoy + other point survive; request consumed)"
else
  printf 'FAIL: delete-request behavior wrong (rc=%s)\n' "$rc" >&2
  printf '  remaining: %s\n' "$(ls "$b" | tr '\n' ' ')" >&2
  printf '  outcome: %s\n' "$(cat "${d}/.last_delete.json" 2>/dev/null)" >&2
  fail=1
fi
rm -rf "$d" "$b"

# 2) A timestamp named by a present .restore_request.json is REFUSED.
d="$(mktemp -d)"; b="$(mktemp -d)"
seed_backups "$b" 20260101_010101
printf '{"timestamps": ["20260101_010101"], "confirm": "DELETE", "version": 1}' > "${d}/.delete_request.json"
printf '{"timestamp": "20260101_010101", "confirm": "RESTORE"}' > "${d}/.restore_request.json"
run_prune "$d" "$b" >/dev/null 2>&1
if ts_present 20260101_010101 "$b" && grep -q 'in-flight restore' "${d}/.last_delete.json" 2>/dev/null; then
  pass "refuses to delete a timestamp an in-flight restore is using"
else
  printf 'FAIL: in-flight restore point was NOT refused\n' >&2; fail=1
fi
rm -rf "$d" "$b"

# 2b) A safety_backup_ts named in a present .restore_status.json is REFUSED.
d="$(mktemp -d)"; b="$(mktemp -d)"
seed_backups "$b" 20260101_010101
printf '{"timestamps": ["20260101_010101"], "confirm": "DELETE", "version": 1}' > "${d}/.delete_request.json"
printf '{"state":"running","safety_backup_ts":"20260101_010101"}' > "${d}/.restore_status.json"
run_prune "$d" "$b" >/dev/null 2>&1
if ts_present 20260101_010101 "$b"; then
  pass "refuses to delete a timestamp used as a restore's safety backup"
else
  printf 'FAIL: safety-backup timestamp was NOT refused\n' >&2; fail=1
fi
rm -rf "$d" "$b"

# 3) Version gate + confirm gate both delete NOTHING and record a reason.
for scenario in "version" "confirm"; do
  d="$(mktemp -d)"; b="$(mktemp -d)"
  seed_backups "$b" 20260101_010101
  if [ "$scenario" = "version" ]; then
    printf '{"timestamps": ["20260101_010101"], "confirm": "DELETE", "version": 2}' > "${d}/.delete_request.json"
    want='unknown version'
  else
    printf '{"timestamps": ["20260101_010101"], "version": 1}' > "${d}/.delete_request.json"
    want='confirm required'
  fi
  run_prune "$d" "$b" >/dev/null 2>&1
  if ts_present 20260101_010101 "$b" && grep -q "$want" "${d}/.last_delete.json" 2>/dev/null; then
    pass "gate '${scenario}' deletes nothing and records reason '${want}'"
  else
    printf 'FAIL: gate %s did not fail safe (outcome: %s)\n' "$scenario" \
      "$(cat "${d}/.last_delete.json" 2>/dev/null)" >&2; fail=1
  fi
  rm -rf "$d" "$b"
done

# === backup.sh — keep-last-N retention (single-sourced helper) ================
checkf "$BACKUP_SCRIPT" "backup.sh reads UI retention (.retention.json)" '\.retention\.json'
checkf "$BACKUP_SCRIPT" "backup.sh honors keep_last_n" 'KEEP_LAST_N'
checkf "$BACKUP_SCRIPT" "backup.sh honors max_age_days for the age window" 'max_age_days'
checkf "$BACKUP_SCRIPT" "backup.sh keeps BACKUP_SKIP_PRUNE honored" 'BACKUP_SKIP_PRUNE'

keepfn="$(sed -n '/^retention_keep_last_n()/,/^}/p' "$BACKUP_SCRIPT")"
run_keep() {
  # $1 dir, $2 keep_n, $3 in_flight-newline-list
  bash -c "set -euo pipefail; ${keepfn}; retention_keep_last_n \"\$1\" \"\$2\" \"\$3\"" \
    _ "$1" "$2" "$3" >/dev/null 2>&1
}
distinct_ts() { ls "$1" 2>/dev/null | grep -oE '[0-9]{8}_[0-9]{6}' | sort -u | tr '\n' ' '; }

# N+2 timestamps, keep_last_n=N -> the 2 oldest are pruned, newest N kept.
b="$(mktemp -d)"
for ts in 20260101_010101 20260102_010101 20260103_010101 20260104_010101; do
  touch "${b}/jarvis_${ts}.sql.gz" "${b}/litellm_${ts}.sql.gz.enc" \
        "${b}/manifest_${ts}.json" "${b}/qdrant_kg_${ts}.snapshot"
done
run_keep "$b" 2 ""
kept="$(distinct_ts "$b")"
if [ "$kept" = "20260103_010101 20260104_010101 " ]; then
  pass "keep-last-N prunes the tail (4 points, keep 2 -> 2 pruned, newest 2 kept)"
else
  printf 'FAIL: keep-last-2 kept the wrong set: [%s]\n' "$kept" >&2; fail=1
fi
rm -rf "$b"

# in-flight restore timestamp is never pruned even when past the keep window.
b="$(mktemp -d)"
for ts in 20260101_010101 20260102_010101 20260103_010101; do
  touch "${b}/jarvis_${ts}.sql.gz" "${b}/manifest_${ts}.json"
done
run_keep "$b" 1 "20260101_010101"
kept="$(distinct_ts "$b")"
if [ "$kept" = "20260101_010101 20260103_010101 " ]; then
  pass "keep-last-N never prunes an in-flight restore timestamp"
else
  printf 'FAIL: keep-last-1 w/ in-flight kept the wrong set: [%s]\n' "$kept" >&2; fail=1
fi
rm -rf "$b"

# keep_last_n=0 is a no-op (retain everything) — the destructive "keep zero"
# reading is floored, symmetric with the max_age_days=0 floor below.
b="$(mktemp -d)"
for ts in 20260101_010101 20260102_010101 20260103_010101; do
  touch "${b}/jarvis_${ts}.sql.gz" "${b}/manifest_${ts}.json"
done
run_keep "$b" 0 ""
kept="$(distinct_ts "$b")"
if [ "$kept" = "20260101_010101 20260102_010101 20260103_010101 " ]; then
  pass "keep_last_n=0 is a no-op (retains every restore point, deletes none)"
else
  printf 'FAIL: keep-last-0 pruned points, kept: [%s]\n' "$kept" >&2; fail=1
fi
rm -rf "$b"

# === backup.sh — max_age_days floor (a stray 0 must NOT become find -mtime +0) =
# A UI max_age_days of 0 must fall back to the env default, never delete-all-old.
resolvefn="$(sed -n '/^resolve_retention_days()/,/^}/p' "$BACKUP_SCRIPT")"
run_resolve() {  # $1 retention-file  $2 env-default  -> effective days on stdout
  bash -c "set -euo pipefail; ${resolvefn}; resolve_retention_days \"\$1\" \"\$2\"" _ "$1" "$2"
}
rdir="$(mktemp -d)"; rf="${rdir}/.retention.json"

printf '{"keep_last_n": null, "max_age_days": 0}' > "$rf"
got="$(run_resolve "$rf" 7)"
if [ "$got" = "7" ]; then
  pass "max_age_days=0 is a no-op (env default stands; no find -mtime +0 -delete)"
else
  printf 'FAIL: max_age_days=0 resolved to [%s], expected env default 7\n' "$got" >&2; fail=1
fi

printf '{"keep_last_n": null, "max_age_days": 5}' > "$rf"
got="$(run_resolve "$rf" 7)"
if [ "$got" = "5" ]; then
  pass "max_age_days>=1 overrides the env default"
else
  printf 'FAIL: max_age_days=5 resolved to [%s], expected 5\n' "$got" >&2; fail=1
fi

printf '{"keep_last_n": 3}' > "$rf"
got="$(run_resolve "$rf" 7)"
if [ "$got" = "7" ]; then
  pass "absent max_age_days falls back to the env default"
else
  printf 'FAIL: absent max_age_days resolved to [%s], expected 7\n' "$got" >&2; fail=1
fi
rm -rf "$rdir"

# === restore.sh — inbox-tar sanitization =====================================
# The old unguarded `decrypt | tar -xzf -` extraction is GONE; the new path lists
# members first and rejects absolute / traversal paths, and extracts with the
# no-same-owner/no-same-permissions hardening.
if grep -q 'tar -xzf - -C "\$SECRETS_STAGING"' "$RESTORE_SCRIPT"; then
  printf 'FAIL: restore.sh still pipes decrypt straight into tar -xzf - (no member check)\n' >&2; fail=1
else
  pass "restore.sh no longer pipes decrypt straight into tar -xzf -"
fi
checkf "$RESTORE_SCRIPT" "restore.sh lists tar members before extracting" 'tar -tzf "\$SECRETS_TAR_TMP"'
checkf "$RESTORE_SCRIPT" "restore.sh rejects absolute / traversal members" "grep -Eq '\\^/"
checkf "$RESTORE_SCRIPT" "restore.sh extracts with --no-same-owner --no-same-permissions" \
  'tar --no-same-owner --no-same-permissions -xzf'

# Behavioral: the exact rejection regex used by restore.sh rejects a real
# malicious tar (traversal / absolute) and accepts a legit relative archive.
UNSAFE_RE='^/|\.\.|^[A-Za-z]:'
grep -qF "grep -Eq '${UNSAFE_RE}'" "$RESTORE_SCRIPT" \
  && pass "the test's rejection regex matches restore.sh's" \
  || { printf 'FAIL: restore.sh rejection regex drifted from the test\n' >&2; fail=1; }
tdir="$(mktemp -d)"
mkdir -p "${tdir}/sec"; printf 'pw' > "${tdir}/sec/postgres_password.txt"
tar -czf "${tdir}/legit.tar.gz" -C "${tdir}/sec" .
tar -czf "${tdir}/trav.tar.gz" -C "${tdir}/sec" postgres_password.txt \
  --transform='s|postgres_password.txt|../../evil|' 2>/dev/null
judge() { # $1 archive, $2 expected(ACCEPT|REJECT)
  local m got
  m="$(tar -tzf "$1" 2>/dev/null || true)"
  # here-string, mirroring restore.sh (no pipe -> no pipefail+SIGPIPE misread).
  if grep -Eq "$UNSAFE_RE" <<<"$m"; then got=REJECT; else got=ACCEPT; fi
  [ "$got" = "$2" ]
}
if judge "${tdir}/legit.tar.gz" ACCEPT && judge "${tdir}/trav.tar.gz" REJECT; then
  pass "inbox-tar guard: accepts a legit relative archive, rejects a '../..' traversal member"
else
  printf 'FAIL: inbox-tar member guard behaved wrong\n' >&2; fail=1
fi
rm -rf "$tdir"

# === docker-compose.yml — loop hardening + delete branch =====================
cmp_check() {
  if grep -Eq "$2" "$COMPOSE"; then pass "$1"; else
    printf 'FAIL: %s (pattern: %s)\n' "$1" "$2" >&2; fail=1; fi
}
cmp_check "sidecar mounts prune.sh (ro)" 'prune\.sh:/usr/local/bin/prune\.sh:ro'
cmp_check "loop drops -e so one failed run cannot kill it (sh -uc)" 'sh -uc "while true'
cmp_check "loop has the delete branch (prune.sh on .delete_request.json)" \
  '\.delete_request\.json.*prune\.sh'
cmp_check "each branch logs its failure and continues (|| echo ... rc=)" \
  '\|\| echo .*\[sidecar\].*rc=\$\$\?'
# the -e flag must be gone from the entrypoint (sh -euc -> sh -uc).
if grep -q 'sh -euc "while true' "$COMPOSE"; then
  printf 'FAIL: the sidecar entrypoint still uses sh -euc (a failed run crash-restarts the loop)\n' >&2; fail=1
else
  pass "the sidecar entrypoint no longer uses sh -euc"
fi
# inner poll early-breaks on ALL THREE sentinels (they co-occur on the folded
# scalar's `.backup_now` line).
poll="$(grep -F '.backup_now' "$COMPOSE" || true)"
if printf '%s' "$poll" | grep -q '\.backup_now' \
   && printf '%s' "$poll" | grep -q '\.restore_request\.json' \
   && printf '%s' "$poll" | grep -q '\.delete_request\.json'; then
  pass "inner poll early-breaks on all three sentinels (.backup_now/.restore_request/.delete_request)"
else
  printf 'FAIL: inner poll does not early-break on all three sentinels\n' >&2; fail=1
fi

# Full compose validation when docker is available; otherwise a YAML lint.
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if ( cd "${SCRIPT_DIR}/../.." && docker compose config >/dev/null ) 2>/dev/null; then
    pass "docker compose config validates"
  else
    printf 'FAIL: docker compose config rejected the compose file\n' >&2; fail=1
  fi
elif command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' "$COMPOSE" 2>/dev/null; then
    pass "docker-compose.yml is well-formed YAML (docker unavailable; lint only)"
  else
    printf 'FAIL: docker-compose.yml is not well-formed YAML\n' >&2; fail=1
  fi
else
  printf 'SKIP: neither docker nor python3 available for compose validation\n' >&2
fi

if [ "$fail" -ne 0 ]; then
  printf '\nprune coverage: FAILED\n' >&2
  exit 1
fi
printf '\nprune coverage: all checks passed\n'
