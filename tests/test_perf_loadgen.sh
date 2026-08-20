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
#   4. Strict evidence mode: dead port → exits 3 and leaves no CSV summary
#   5. OUT_DIR is honoured and loadgen-summary.csv / loadgen-concurrency.csv
#      are NOT created when the server is unreachable (no partial files)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOADGEN="${SCRIPT_DIR}/../scripts/perf/loadgen.sh"
PROFILE="${SCRIPT_DIR}/../scripts/profile.sh"

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
  JARVIS_BASE_URL="http://127.0.0.1:${DEAD_PORT}" \
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

# ---- 4. Strict failure against dead port -----------------------------------
set +e
strict_output=$(\
  LOADGEN_STRICT=1 \
  JARVIS_BASE_URL="http://127.0.0.1:${DEAD_PORT}" \
  OUT_DIR="${TMP_OUT}" \
  bash "${LOADGEN}" 2>&1
)
strict_exit_code=$?
set -e

if [[ ${strict_exit_code} -eq 3 ]]; then
  pass "strict mode exits 3 when server unreachable"
else
  fail "strict mode exited ${strict_exit_code}, expected 3"
fi

if [[ ! -f "${TMP_OUT}/loadgen-concurrency.csv" ]] && \
   [[ ! -f "${TMP_OUT}/loadgen-summary.csv" ]] && \
   echo "${strict_output}" | grep -q "product gateway not reachable"; then
  pass "strict failure leaves no successful-looking CSV"
else
  fail "strict failure left CSV output or omitted its reason"
fi

# ---- 5. No partial output files when server unreachable --------------------
if [[ ! -f "${TMP_OUT}/loadgen-concurrency.csv" ]] && \
   [[ ! -f "${TMP_OUT}/loadgen-summary.csv" ]]; then
  pass "no partial output files created on skip"
else
  fail "unexpected output files created despite server being unreachable"
fi

# ---- 6. Strict mode invalidates stale failure evidence ----------------------
if grep -Fq 'rm -f "${OUT_DIR}/loadgen-FATAL.txt"' "${LOADGEN}"; then
  pass "strict mode clears stale fatal evidence before capture"
else
  fail "strict mode does not clear stale fatal evidence"
fi

# ---- 7. Supported profile caller preserves the gateway boundary ------------
if grep -Fq 'JARVIS_BASE_URL="${JARVIS_BASE_URL}"' "${PROFILE}" && \
   grep -Fq -- '-b "${PROFILE_COOKIE_JAR}"' "${PROFILE}" && \
   [[ "$(grep -Fc -- '-H "X-API-Key: ${API_KEY}"' "${PROFILE}")" -eq 1 ]] && \
   ! grep -Fq 'PAPER_INGESTION_HOST_PORT="${PAPER_INGESTION_HOST_PORT:-8010}"' "${PROFILE}"; then
  pass "profile uses the product gateway and owner session"
else
  fail "profile does not preserve the gateway session boundary"
fi

# ---- 8. User-scoped load uses the exchanged owner session ------------------
if grep -Fq -- '-b "${COOKIE_JAR}"' "${LOADGEN}" && \
   [[ "$(grep -Fc -- '-H "X-API-Key: ${API_KEY}"' "${LOADGEN}")" -eq 1 ]]; then
  pass "user-scoped requests use the exchanged owner session"
else
  fail "raw deployment key escaped the session-exchange boundary"
fi

# ---- 9. Strict sustained load uses a fixed sample population ---------------
if grep -Fq 'for (( sustain_batch=1; sustain_batch<=LOADGEN_SUSTAIN_BATCHES; sustain_batch++ ))' "${LOADGEN}" && \
   grep -Fq '_is_positive_integer "${LOADGEN_SUSTAIN_BATCHES}"' "${LOADGEN}"; then
  pass "strict sustained load uses a validated fixed batch count"
else
  fail "strict sustained load still depends on elapsed wall time"
fi

# ---- 10. Strict hot paths support fixed, validated sample populations -------
if grep -Fq '_is_positive_integer "${SEARCH_BATCHES}"' "${LOADGEN}" && \
   grep -Fq '_is_positive_integer "${RAG_BATCHES}"' "${LOADGEN}" && \
   grep -Fq 'while [[ ${_search_b} -le ${SEARCH_BATCHES} ]]' "${LOADGEN}"; then
  pass "strict hot paths use validated fixed batch counts"
else
  fail "strict hot-path sample population is not configurable and fixed"
fi

if grep -Fq '_is_positive_integer "${LOADGEN_FANOUT_BATCHES}"' "${LOADGEN}" && \
   grep -Fq 'fanout_batch<=LOADGEN_FANOUT_BATCHES' "${LOADGEN}"; then
  pass "strict fan-out uses a validated fixed batch count"
else
  fail "strict fan-out sample population is not configurable and fixed"
fi

# ---- 11. Strict comparison excludes asynchronous background model work ------
if grep -Fq 'if [[ "${LOADGEN_STRICT}" != "1" ]]; then' "${LOADGEN}" && \
   grep -Fq 'POST /api/pulse/generate' "${LOADGEN}"; then
  pass "strict comparison excludes the asynchronous Pulse job"
else
  fail "strict comparison can be contaminated by asynchronous Pulse work"
fi

# ---- 12. Throughput uses sub-second wall-clock precision ---------------------
if grep -Fq '_elapsed_seconds()' "${LOADGEN}" && \
   grep -Fq 'date +%s%N' "${LOADGEN}" && \
   ! grep -Fq 'A_ELAPSED=$(( A_END - A_START ))' "${LOADGEN}"; then
  pass "throughput uses high-resolution elapsed time"
else
  fail "throughput is quantized to whole seconds"
fi

# ---- 13. Fixed comparisons warm identical routes before timing ---------------
if grep -Fq '_warm_get_path "${endpoint}"' "${LOADGEN}" && \
   grep -Fq '_warm_post_path "/api/papers/search-hybrid"' "${LOADGEN}" && \
   grep -Fq '_warm_post_path "/api/ask"' "${LOADGEN}" && \
   grep -Fq 'PERF_WARM_SETTLE_SECS=6' "${LOADGEN}"; then
  pass "fixed comparisons warm identical routes before timing"
else
  fail "fixed comparisons include asymmetric cold-start work"
fi

# ---- Summary ---------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ ${FAIL} -gt 0 ]]; then
  exit 1
fi
exit 0
