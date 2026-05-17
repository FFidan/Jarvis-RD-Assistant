#!/usr/bin/env bash
# tests/test_perf_loadgen.sh — Minimal shell self-check for scripts/perf/loadgen.sh
#
# Run manually:
#   bash tests/test_perf_loadgen.sh
#
# Not wired into pytest or the CI gate — kept lean and standalone.
# Checks:
#   1. Bash syntax passes (bash -n)
#   2. Script is executable
#   3. Graceful-degradation: pointing at a dead port → warns, exits 0
#   4. OUT_DIR is honoured and loadgen-summary.csv / loadgen-concurrency.csv
#      are NOT created when the server is unreachable (no partial files)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOADGEN="${SCRIPT_DIR}/../scripts/perf/loadgen.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $*"; PASS=$(( PASS + 1 )); }
fail() { echo "  FAIL: $*"; FAIL=$(( FAIL + 1 )); }

echo "=== test_perf_loadgen: loadgen.sh self-check ==="

# ---- 1. Syntax check -------------------------------------------------------
if bash -n "${LOADGEN}" 2>&1; then
  pass "bash -n (syntax OK)"
else
  fail "bash -n failed"
fi

# ---- 2. Executable bit -----------------------------------------------------
if [[ -x "${LOADGEN}" ]]; then
  pass "executable bit set"
else
  fail "not executable"
fi

# ---- 3. Graceful degradation against dead port -----------------------------
# Use a port that is almost certainly not listening (62999)
DEAD_PORT=62999
TMP_OUT="$(mktemp -d)"
trap 'rm -rf "${TMP_OUT}"' EXIT

# PERF_SUSTAIN_SECS=1 just in case the server IS somehow up on that port;
# we also override PERF_CONCURRENCY to 1 for speed.
set +e
output=$(
  PAPER_INGESTION_HOST_PORT="${DEAD_PORT}" \
  OUT_DIR="${TMP_OUT}" \
  PERF_CONCURRENCY=1 \
  PERF_SUSTAIN_SECS=1 \
  bash "${LOADGEN}" 2>&1
)
exit_code=$?
set -e

if [[ ${exit_code} -eq 0 ]]; then
  pass "exits 0 when server unreachable"
else
  fail "non-zero exit (${exit_code}) when server unreachable"
fi

if echo "${output}" | grep -q "WARN:"; then
  pass "emits WARN when server unreachable"
else
  fail "no WARN message emitted"
fi

# ---- 4. No partial output files when server unreachable --------------------
if [[ ! -f "${TMP_OUT}/loadgen-concurrency.csv" ]] && \
   [[ ! -f "${TMP_OUT}/loadgen-summary.csv" ]]; then
  pass "no partial output files created on skip"
else
  fail "unexpected output files created despite server being unreachable"
fi

# ---- Summary ---------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ ${FAIL} -gt 0 ]]; then
  exit 1
fi
exit 0
