#!/usr/bin/env bash
# scripts/perf/loadgen.sh — Concurrency load driver for JARVIS perf harness.
#
# Drives two scenarios additive to the sequential timings in scripts/profile.sh:
#
#   Scenario A — dashboard fan-out:
#     Fires PERF_CONCURRENCY concurrent curl requests per route across the same
#     GET endpoint set that profile.sh times sequentially.  Simulates a burst
#     of N browser tabs all hitting the API at once.
#
#   Scenario B — Pulse sustained-load:
#     Repeats PERF_CONCURRENCY concurrent requests against the /health and
#     /api/papers/feed endpoints over PERF_SUSTAIN_SECS seconds.  Simulates
#     Pulse background-refresh traffic.
#
# Environment knobs (all optional — sensible defaults shown):
#   PERF_CONCURRENCY       Number of simultaneous curl workers per batch.
#                          Default: 10.  Set lower (e.g. 3) for quick CI runs
#                          or higher for stress testing.
#   PERF_SUSTAIN_SECS      Duration of the Scenario B sustained window.
#                          Default: 15 seconds.
#   OUT_DIR                Where to write output files.
#                          Default: artifacts/perf/<UTC-timestamp>/ (auto-created).
#   PAPER_INGESTION_HOST_PORT  Override the backend port (default 8010).
#
# Output (additive — never overwrites backend-timings.csv):
#   loadgen-concurrency.csv   per-request latencies for both scenarios
#   loadgen-summary.csv       p50/p95/p99 + throughput (req/s) per scenario
#
# Contract: never fails the build — unreachable server / missing tools log
# warnings and the script exits 0.
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------
# REPO_ROOT: this file lives in scripts/perf/ → go up two levels
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# Honour an externally-provided OUT_DIR (e.g. from profile.sh passing its own
# artifact dir).  Otherwise create a fresh timestamped directory.
OUT_DIR="${OUT_DIR:-${REPO_ROOT}/artifacts/perf/${TIMESTAMP}}"
mkdir -p "${OUT_DIR}"

# -- API key -----------------------------------------------------------------
API_KEY_FILE="${REPO_ROOT}/secrets/jarvis_api_key.txt"
API_KEY=""
if [[ -f "${API_KEY_FILE}" ]]; then
  API_KEY="$(cat "${API_KEY_FILE}")"
fi

# -- Backend base URL --------------------------------------------------------
PAPER_INGEST_PORT="${PAPER_INGESTION_HOST_PORT:-8010}"
PAPER_INGEST_BASE="http://localhost:${PAPER_INGEST_PORT}"

# -- Concurrency knobs -------------------------------------------------------
# PERF_CONCURRENCY: number of parallel curl workers fired per batch.
# 10 is representative of a typical dashboard fan-out (5-15 open browser tabs).
# Kept small enough to run on a dev laptop without saturating the loopback NIC.
PERF_CONCURRENCY="${PERF_CONCURRENCY:-10}"

# PERF_SUSTAIN_SECS: how long Scenario B fires repeated concurrent batches.
PERF_SUSTAIN_SECS="${PERF_SUSTAIN_SECS:-15}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[loadgen] $*" >&2; }

# Output files (additive — separate from backend-timings.csv)
DETAIL_CSV="${OUT_DIR}/loadgen-concurrency.csv"
SUMMARY_CSV="${OUT_DIR}/loadgen-summary.csv"

# Temp directory for per-worker result files
TMPDIR_LG="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_LG}"' EXIT

# ---------------------------------------------------------------------------
# Reachability guard — degrade gracefully when the server is not running
# ---------------------------------------------------------------------------
if ! curl -s -o /dev/null --max-time 2 "${PAPER_INGEST_BASE}/health" \
     -H "X-API-Key: ${API_KEY}" -H "Accept: application/json" 2>/dev/null; then
  log "WARN: backend not reachable at ${PAPER_INGEST_BASE} — skipping loadgen scenarios"
  log "      Start the stack with 'make up' and re-run to collect concurrency metrics."
  exit 0
fi

# ---------------------------------------------------------------------------
# Core: fire PERF_CONCURRENCY concurrent curl requests against one endpoint,
# write each result as "scenario,endpoint,seconds,http_code" into TMPDIR_LG,
# then wait for all workers to finish.
#
# Args: $1 = scenario_label  $2 = endpoint_path
# ---------------------------------------------------------------------------
_fire_batch() {
  local scenario="$1"
  local path="$2"
  local i
  for (( i=1; i<=PERF_CONCURRENCY; i++ )); do
    (
      result=$(curl -s -o /dev/null \
                    --max-time 30 \
                    -w "%{time_total},%{http_code}" \
                    "${PAPER_INGEST_BASE}${path}" \
                    -H "X-API-Key: ${API_KEY}" \
                    -H "Accept: application/json" 2>/dev/null \
               || echo "ERR,000")
      echo "${scenario},${path},${result}"
    ) > "${TMPDIR_LG}/result_${i}.txt" &
  done
  wait  # wait for all background workers in this batch
  # Concatenate per-worker files into a single results file; each worker wrote
  # to its own file so there is no concurrent-append race on a shared fd.
  cat "${TMPDIR_LG}"/result_*.txt > "${TMPDIR_LG}/results.txt" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Percentile helper — pure POSIX sort + awk (no python, no GNU-only flags)
#
# Args: $1 = label  $2 = tempfile containing one float per line
# Writes one CSV row: label,count,p50,p95,p99,throughput_rps
# ---------------------------------------------------------------------------
_emit_stats() {
  local label="$1"
  local datafile="$2"
  local elapsed_s="$3"   # wall-clock window used to compute req/s

  # Sort the raw latency values numerically
  sort -n "${datafile}" > "${TMPDIR_LG}/sorted_${label}.txt"

  awk -v label="${label}" -v elapsed="${elapsed_s}" '
  BEGIN { n=0 }
  {
    vals[n] = $1
    n++
  }
  END {
    if (n == 0) {
      print label ",0,0,0,0,0"
      exit
    }
    # Percentile index (nearest-rank — works on any awk, no GNU required)
    function pct(p,   idx) {
      idx = int(p/100 * n + 0.999999) - 1
      if (idx < 0) idx = 0
      if (idx >= n) idx = n-1
      return vals[idx]
    }
    p50 = pct(50)
    p95 = pct(95)
    p99 = pct(99)
    rps = (elapsed > 0) ? n / elapsed : 0
    printf "%s,%d,%.4f,%.4f,%.4f,%.2f\n", label, n, p50, p95, p99, rps
  }
  ' "${TMPDIR_LG}/sorted_${label}.txt"
}

# ---------------------------------------------------------------------------
# Write CSV headers
# ---------------------------------------------------------------------------
echo "scenario,endpoint,seconds,http_code" > "${DETAIL_CSV}"
echo "scenario,requests,p50_s,p95_s,p99_s,throughput_rps" > "${SUMMARY_CSV}"

# ---------------------------------------------------------------------------
# Scenario A — Dashboard fan-out
# Fire PERF_CONCURRENCY concurrent requests per route; same endpoint set as
# profile.sh sequential section.
# ---------------------------------------------------------------------------
log "Scenario A: dashboard fan-out (${PERF_CONCURRENCY} concurrent per route)"
A_ENDPOINTS=(
  "/api/papers/feed?limit=20"
  "/api/papers/brief"
  "/api/dashboard/metrics"
  "/api/system/models"
  "/health"
)

A_START="$(date +%s)"
: > "${TMPDIR_LG}/scen_a_latencies.txt"   # empty latency collector

for endpoint in "${A_ENDPOINTS[@]}"; do
  log "  → ${endpoint} (×${PERF_CONCURRENCY} concurrent)"

  # Clean up per-worker files from any prior batch; _fire_batch writes
  # result_<i>.txt per worker and then concatenates them into results.txt.
  rm -f "${TMPDIR_LG}"/result_*.txt
  _fire_batch "scenario_a" "${endpoint}"

  # Append detail rows to the CSV and extract latencies for stats
  while IFS= read -r row; do
    echo "${row}" >> "${DETAIL_CSV}"
    # row format: scenario_a,/path,<seconds>,<code>  — extract field 3
    lat=$(echo "${row}" | awk -F',' '{print $3}')
    if [[ "${lat}" != "ERR" ]]; then
      echo "${lat}" >> "${TMPDIR_LG}/scen_a_latencies.txt"
    fi
  done < "${TMPDIR_LG}/results.txt"
done

A_END="$(date +%s)"
A_ELAPSED=$(( A_END - A_START ))
[[ ${A_ELAPSED} -le 0 ]] && A_ELAPSED=1   # guard divide-by-zero

_emit_stats "scenario_a_fanout" "${TMPDIR_LG}/scen_a_latencies.txt" "${A_ELAPSED}" \
  >> "${SUMMARY_CSV}"
log "Scenario A complete in ${A_ELAPSED}s"

# ---------------------------------------------------------------------------
# Scenario B — Pulse sustained-load
# Repeat concurrent batches against /health + /api/papers/feed for
# PERF_SUSTAIN_SECS seconds.  Simulates Pulse refresh background traffic.
# ---------------------------------------------------------------------------
log "Scenario B: Pulse sustained-load (${PERF_SUSTAIN_SECS}s window, ${PERF_CONCURRENCY} concurrent)"
PULSE_ENDPOINTS=(
  "/health"
  "/api/papers/feed?limit=20"
)

: > "${TMPDIR_LG}/scen_b_latencies.txt"
B_START="$(date +%s)"
B_END_DEADLINE=$(( B_START + PERF_SUSTAIN_SECS ))

while [[ "$(date +%s)" -lt "${B_END_DEADLINE}" ]]; do
  for endpoint in "${PULSE_ENDPOINTS[@]}"; do
    rm -f "${TMPDIR_LG}"/result_*.txt
    _fire_batch "scenario_b" "${endpoint}"

    while IFS= read -r row; do
      echo "${row}" >> "${DETAIL_CSV}"
      lat=$(echo "${row}" | awk -F',' '{print $3}')
      if [[ "${lat}" != "ERR" ]]; then
        echo "${lat}" >> "${TMPDIR_LG}/scen_b_latencies.txt"
      fi
    done < "${TMPDIR_LG}/results.txt"
  done
done

B_END="$(date +%s)"
B_ELAPSED=$(( B_END - B_START ))
[[ ${B_ELAPSED} -le 0 ]] && B_ELAPSED=1

_emit_stats "scenario_b_pulse" "${TMPDIR_LG}/scen_b_latencies.txt" "${B_ELAPSED}" \
  >> "${SUMMARY_CSV}"
log "Scenario B complete in ${B_ELAPSED}s"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "Load-gen artifacts:"
log "  detail  → ${DETAIL_CSV}"
log "  summary → ${SUMMARY_CSV}"
