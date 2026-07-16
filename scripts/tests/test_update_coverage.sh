#!/usr/bin/env bash
# test_update_coverage.sh — behavioral tests for update.sh's post-update
# failure report: the "Third-party services" (versions.env) recovery
# block and the "Application services" (JARVIS_VERSION) recovery block must
# each print ONLY when their half of the failed-service set is non-empty, and
# the JARVIS_VERSION rollback commands must name only application services
# (FAILED_APP), never a third-party pin.
#
# Technique (mirrors scripts/tests/test_setup_lib_helpers.sh's preflight_disk
# extraction at its `pf_src="$(sed -n ...)"` line): sed-extract update.sh's
# report block as inline text and eval it inside a private `bash -c` with a
# stubbed FAILED array + the script's color/log helpers. No docker, no live
# services, no network.
#
# Run: bash scripts/tests/test_update_coverage.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATE_SCRIPT="${SCRIPT_DIR}/../../update.sh"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }

# The report block runs at top level (not inside a function), so it is
# extracted by anchor lines rather than a `funcname() { ... }` pattern: from
# the "any failures?" guard through the final `exit 1`. Both anchors are
# unique in update.sh (verified below).
report_src="$(sed -n '/^if \[ "\${#FAILED\[@\]}" -eq 0 \]/,/^exit 1$/p' "$UPDATE_SCRIPT")"
if [ -z "$report_src" ]; then
  printf 'FAIL: could not sed-extract the report block from %s\n' "$UPDATE_SCRIPT" >&2
  exit 1
fi

# run_report <failed-service...> -> prints the report block's stdout; the
# process exit code (1 for a non-empty FAILED set) is left in $?.
run_report() {
  FAILED_LIST="$*" bash -c '
    set -uo pipefail
    C_BOLD=""; C_RESET=""
    warn() { :; }
    ok()   { :; }
    read -r -a FAILED <<< "$FAILED_LIST"
    '"$report_src"'
  '
}

# === app-only: FAILED=(dashboard) ============================================
out="$(run_report dashboard)" && rc=0 || rc=$?
[ "$rc" -eq 1 ] && pass "app-only: report exits 1" \
  || { printf 'FAIL: app-only report did not exit 1 (rc=%s)\n' "$rc" >&2; fail=1; }
if ! printf '%s' "$out" | grep -q 'Third-party services'; then
  pass "app-only FAILED set: no third-party/versions.env block"
else
  printf 'FAIL: app-only FAILED set printed the third-party block\n' >&2; fail=1
fi
if printf '%s' "$out" | grep -q 'Application services'; then
  pass "app-only FAILED set: prints the application/JARVIS_VERSION block"
else
  printf 'FAIL: app-only FAILED set did not print the application block\n' >&2; fail=1
fi
if printf '%s' "$out" | grep -q 'JARVIS_VERSION=<previous-version> docker compose pull dashboard'; then
  pass "app-only FAILED set: JARVIS_VERSION line names the app service"
else
  printf 'FAIL: app-only JARVIS_VERSION line missing/wrong (out=%s)\n' "$out" >&2; fail=1
fi
if printf '%s' "$out" | grep -q 'docker compose logs --tail=200 dashboard'; then
  pass "app-only FAILED set: trailing logs line lists the full FAILED set"
else
  printf 'FAIL: app-only trailing logs line missing/wrong (out=%s)\n' "$out" >&2; fail=1
fi

# === third-party-only: FAILED=(postgres) =====================================
out="$(run_report postgres)" && rc=0 || rc=$?
[ "$rc" -eq 1 ] && pass "third-party-only: report exits 1" \
  || { printf 'FAIL: third-party-only report did not exit 1 (rc=%s)\n' "$rc" >&2; fail=1; }
if printf '%s' "$out" | grep -q 'Third-party services'; then
  pass "third-party-only FAILED set: prints the third-party/versions.env block"
else
  printf 'FAIL: third-party-only FAILED set did not print the third-party block\n' >&2; fail=1
fi
if ! printf '%s' "$out" | grep -q 'Application services'; then
  pass "third-party-only FAILED set: no application/JARVIS_VERSION block"
else
  printf 'FAIL: third-party-only FAILED set printed the application block\n' >&2; fail=1
fi
if ! printf '%s' "$out" | grep -q 'JARVIS_VERSION='; then
  pass "third-party-only FAILED set: no JARVIS_VERSION line at all"
else
  printf 'FAIL: third-party-only FAILED set printed a JARVIS_VERSION line (out=%s)\n' "$out" >&2; fail=1
fi
if printf '%s' "$out" | grep -q 'docker compose logs --tail=200 postgres'; then
  pass "third-party-only FAILED set: trailing logs line lists the full FAILED set"
else
  printf 'FAIL: third-party-only trailing logs line missing/wrong (out=%s)\n' "$out" >&2; fail=1
fi

# === mixed: FAILED=(dashboard postgres) ======================================
out="$(run_report dashboard postgres)" && rc=0 || rc=$?
[ "$rc" -eq 1 ] && pass "mixed: report exits 1" \
  || { printf 'FAIL: mixed report did not exit 1 (rc=%s)\n' "$rc" >&2; fail=1; }
if printf '%s' "$out" | grep -q 'Third-party services'; then
  pass "mixed FAILED set: prints the third-party/versions.env block"
else
  printf 'FAIL: mixed FAILED set did not print the third-party block\n' >&2; fail=1
fi
if printf '%s' "$out" | grep -q 'Application services'; then
  pass "mixed FAILED set: prints the application/JARVIS_VERSION block"
else
  printf 'FAIL: mixed FAILED set did not print the application block\n' >&2; fail=1
fi
version_lines="$(printf '%s\n' "$out" | grep 'JARVIS_VERSION=')"
if [ -n "$version_lines" ] && ! printf '%s\n' "$version_lines" \
    | grep -qE 'postgres|ollama|qdrant|litellm|cloudflared'; then
  pass "mixed FAILED set: neither JARVIS_VERSION line names a third-party service"
else
  printf 'FAIL: mixed JARVIS_VERSION lines named a third-party service (%s)\n' "$version_lines" >&2
  fail=1
fi
if printf '%s' "$out" | grep -q 'JARVIS_VERSION=<previous-version> docker compose pull dashboard'; then
  pass "mixed FAILED set: JARVIS_VERSION line names the app service"
else
  printf 'FAIL: mixed JARVIS_VERSION line missing/wrong (out=%s)\n' "$out" >&2; fail=1
fi
if printf '%s' "$out" | grep -q 'docker compose logs --tail=200 dashboard postgres'; then
  pass "mixed FAILED set: trailing logs line lists the full (both) FAILED set"
else
  printf 'FAIL: mixed trailing logs line missing/wrong (out=%s)\n' "$out" >&2; fail=1
fi

# =============================================================================

if [ "$fail" -ne 0 ]; then
  printf '\nupdate.sh coverage: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nupdate.sh coverage: all %s checks passed\n' "$pass_n"
