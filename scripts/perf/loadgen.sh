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
#   Scenario B — sustained GET load:
#     Repeats PERF_CONCURRENCY concurrent requests against the /health and
#     /api/papers/feed endpoints over PERF_SUSTAIN_SECS seconds.  Simulates
#     sustained background-refresh traffic.  (Historically mislabelled
#     "Pulse" — it does NOT drive Pulse; Scenario C does.)
#
#   Scenario C — authenticated LLM hot-path drive:
#     Reuses the real owner session minted before Scenario A via
#     POST /api/auth/api-key-session (no email/magic-link needed), then drives
#     the four flag-gated
#     perf_probe.py sites so they actually emit spans:
#       • POST /api/papers/search-hybrid → embed_texts_post + hybrid_search_bm25_sql
#       • POST /api/ask (decompose)      → prepare_cross_paper_rag + embed_texts_post
#       • POST /api/pulse/generate       → pulse_stage2_llm (async worker; 3/hour)
#     Probe JSONL is written container-side and collected by profile.sh.
#     Degrades gracefully (logs + skips, exit 0) if the session can't be
#     minted (multi-tenant / no admin) or the corpus is empty.
#
# Environment knobs (all optional — sensible defaults shown):
#   PERF_CONCURRENCY       Number of simultaneous curl workers per batch.
#                          Default: 10.  Set lower (e.g. 3) for quick CI runs
#                          or higher for stress testing.
#   PERF_SUSTAIN_SECS      Duration of the Scenario B sustained window.
#                          Default: 15 seconds.
#   LOADGEN_SUSTAIN_BATCHES
#                          Fixed Scenario B cycles in strict evidence mode.
#                          Default: 1.
#   RAG_CONCURRENCY        Concurrency for the heavy Scenario C /api/ask
#                          (cross-paper RAG) batch.  Default: min(PERF_CONCURRENCY,3)
#                          — RAG is LLM-bound; a high fan-out just queues on the
#                          single shared ollama and skews latency.
#   SEARCH_BATCHES         Back-to-back hybrid-search batches. Default: 1.
#   RAG_BATCHES            Back-to-back RAG batches. Default: 1.
#   OUT_DIR                Where to write output files.
#                          Default: artifacts/perf/<UTC-timestamp>/ (auto-created).
#   JARVIS_BASE_URL         Product gateway URL (default http://localhost:3001).
#
# Output (additive — never overwrites backend-timings.csv):
#   loadgen-concurrency.csv   per-request latencies for both scenarios
#   loadgen-summary.csv       p50/p95/p99 + throughput (req/s) per scenario
#
# Contract: default mode never fails the build — unreachable server / missing
# tools log warnings and the script exits 0. LOADGEN_STRICT=1 is the explicit
# release-evidence mode and fails closed instead.
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

# -- Product gateway ---------------------------------------------------------
# The fixed profile exercises the same Platform assertion and owner-service
# path as a browser. Direct Research access would bypass the authentication hop
# and cannot mint the owner session used by Scenario C.
JARVIS_BASE_URL="${JARVIS_BASE_URL:-http://localhost:${DASHBOARD_HOST_PORT:-3001}}"

# -- Concurrency knobs -------------------------------------------------------
# PERF_CONCURRENCY: number of parallel curl workers fired per batch.
# 10 is representative of a typical dashboard fan-out (5-15 open browser tabs).
# Kept small enough to run on a dev laptop without saturating the loopback NIC.
PERF_CONCURRENCY="${PERF_CONCURRENCY:-10}"

# PERF_SUSTAIN_SECS: how long Scenario B fires repeated concurrent batches.
PERF_SUSTAIN_SECS="${PERF_SUSTAIN_SECS:-15}"
LOADGEN_SUSTAIN_BATCHES="${LOADGEN_SUSTAIN_BATCHES:-1}"

# RAG_CONCURRENCY: Scenario C /api/ask fan-out. RAG is LLM-bound on one shared
# ollama; default to a small value (≤3) so the probe measures real per-request
# latency, not head-of-line queueing behind a deep request backlog.
if [[ -n "${RAG_CONCURRENCY:-}" ]]; then
  :
elif (( PERF_CONCURRENCY < 3 )); then
  RAG_CONCURRENCY="${PERF_CONCURRENCY}"
else
  RAG_CONCURRENCY=3
fi

# LOADGEN_STRICT: opt-in (set only by the confirmatory bench, never by
# profile.sh / make profile). When 1, the three silent-degrade exits below
# write a one-line reason to ${OUT_DIR}/loadgen-FATAL.txt and exit 3 instead
# of exit 0, so a non-runnable Scenario C aborts the caller loudly instead of
# yielding an empty/NA C1 discovered hours later. Default 0 = exact legacy
# behaviour (backward-compatible with the existing profiling workflow).
LOADGEN_STRICT="${LOADGEN_STRICT:-0}"

# LOADGEN_MIN_SAMPLES applies only to strict evidence. It is intentionally
# separate from the developer-friendly defaults so ordinary profiling remains
# backward-compatible.
LOADGEN_MIN_SAMPLES="${LOADGEN_MIN_SAMPLES:-1}"
LOADGEN_FANOUT_BATCHES="${LOADGEN_FANOUT_BATCHES:-1}"
if [[ -n "${PERF_WARM_SETTLE_SECS:-}" ]]; then
  :
elif [[ "${LOADGEN_STRICT}" == "1" ]]; then
  PERF_WARM_SETTLE_SECS=6
else
  PERF_WARM_SETTLE_SECS=0
fi

# A prior failed capture must not survive a new strict run that later passes.
if [[ "${LOADGEN_STRICT}" == "1" ]]; then
  rm -f "${OUT_DIR}/loadgen-FATAL.txt"
fi

# RAG_BATCHES: how many back-to-back batches of RAG_CONCURRENCY /api/ask
# requests to fire for scenario_c_rag. Default 1 = EXACT legacy behaviour
# (n = RAG_CONCURRENCY → make profile unchanged). The confirmatory bench
# sets this >1 so scenario_c_rag_ask p95 is computed over n =
# RAG_BATCHES×RAG_CONCURRENCY samples instead of a statistically
# meaningless "max of 4/8".
RAG_BATCHES="${RAG_BATCHES:-1}"
SEARCH_BATCHES="${SEARCH_BATCHES:-1}"

# _strict_or_skip <reason> : in strict mode, record + exit 3; else return 0
# so the caller's legacy "log WARN + exit 0" path runs unchanged.
_strict_or_skip() {
  if [[ "${LOADGEN_STRICT}" == "1" ]]; then
    rm -f "${DETAIL_CSV}" "${SUMMARY_CSV}"
    echo "loadgen FATAL: $*" > "${OUT_DIR}/loadgen-FATAL.txt"
    exit 3
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[loadgen] $*" >&2; }

_is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

_is_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

_is_decimal() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

# Output files (additive — separate from backend-timings.csv)
DETAIL_CSV="${OUT_DIR}/loadgen-concurrency.csv"
SUMMARY_CSV="${OUT_DIR}/loadgen-summary.csv"

if [[ "${LOADGEN_STRICT}" == "1" ]] && ! _is_positive_integer "${LOADGEN_MIN_SAMPLES}"; then
  _strict_or_skip "LOADGEN_MIN_SAMPLES must be a positive integer"
fi
if [[ "${LOADGEN_STRICT}" == "1" ]] && ! _is_positive_integer "${LOADGEN_SUSTAIN_BATCHES}"; then
  _strict_or_skip "LOADGEN_SUSTAIN_BATCHES must be a positive integer"
fi
if [[ "${LOADGEN_STRICT}" == "1" ]] && ! _is_positive_integer "${LOADGEN_FANOUT_BATCHES}"; then
  _strict_or_skip "LOADGEN_FANOUT_BATCHES must be a positive integer"
fi
if [[ "${LOADGEN_STRICT}" == "1" ]] && ! _is_positive_integer "${SEARCH_BATCHES}"; then
  _strict_or_skip "SEARCH_BATCHES must be a positive integer"
fi
if [[ "${LOADGEN_STRICT}" == "1" ]] && ! _is_positive_integer "${RAG_BATCHES}"; then
  _strict_or_skip "RAG_BATCHES must be a positive integer"
fi
if [[ "${LOADGEN_STRICT}" == "1" ]] && ! _is_nonnegative_integer "${PERF_WARM_SETTLE_SECS}"; then
  _strict_or_skip "PERF_WARM_SETTLE_SECS must be a non-negative integer"
fi

# Temp directory for per-worker result files
TMPDIR_LG="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_LG}"' EXIT
COOKIE_JAR="${TMPDIR_LG}/jarvis_session.cookies"

# ---------------------------------------------------------------------------
# Reachability guard — degrade gracefully when the server is not running.
# Probe /health/live, NOT /health: /health does heavy dependency fan-out
# (LiteLLM→LLM, Ollama, Qdrant) that routinely exceeds a 2s budget under load
# or while a model is warming, yielding a FALSE "unreachable" that aborts the
# whole run before any stats are emitted (empty loadgen-summary.csv → NA
# metrics). /health/live is the purpose-built, rate-limiter-exempt liveness
# route (added 2026-05-17) — it answers in milliseconds regardless of deps.
# Timeout raised to 10s for slow-boot / GPU-saturated boxes.
# ---------------------------------------------------------------------------
if ! curl -s -o /dev/null --max-time 10 "${JARVIS_BASE_URL}/health/live" \
     -H "Accept: application/json" 2>/dev/null; then
  log "WARN: product gateway not reachable at ${JARVIS_BASE_URL} — skipping loadgen scenarios"
  log "      Start the stack with 'make up' and re-run to collect concurrency metrics."
  _strict_or_skip "product gateway not reachable at ${JARVIS_BASE_URL}/health/live"
  exit 0
fi

# Exchange the deployment API key once for the same owner session used by a
# browser. User-scoped routes intentionally reject the raw key.
auth_code=$(curl -s -o "${TMPDIR_LG}/auth_resp.json" -w '%{http_code}' \
              --max-time 15 -X POST "${JARVIS_BASE_URL}/api/auth/api-key-session" \
              -H "X-API-Key: ${API_KEY}" -H "Content-Type: application/json" \
              -c "${COOKIE_JAR}" -d '{}' 2>/dev/null || echo "000")
if [[ "${auth_code}" != "200" ]] || ! grep -q 'jarvis_session' "${COOKIE_JAR}" 2>/dev/null; then
  log "WARN: api-key-session → HTTP ${auth_code} (need single-tenant + an admin"
  log "      user, or API_KEY_LOGIN_ENABLED). Skipping authenticated load."
  _strict_or_skip "owner session mint failed (api-key-session HTTP ${auth_code})"
  exit 0
fi
log "Owner session minted"

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
                    "${JARVIS_BASE_URL}${path}" \
                    -b "${COOKIE_JAR}" \
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
# Scenario-C variant: fire N concurrent authenticated JSON POSTs.
# Uses the jarvis_session cookie jar (not X-API-Key) so user-scoped endpoints
# accept the request and the real LLM hot-paths execute.
#
# Args: $1=scenario_label  $2=path  $3=json_body  $4=concurrency
# ---------------------------------------------------------------------------
_fire_post_batch() {
  local scenario="$1" path="$2" body="$3" conc="$4" i
  rm -f "${TMPDIR_LG}"/result_*.txt
  for (( i=1; i<=conc; i++ )); do
    (
      result=$(curl -s -o /dev/null \
                    --max-time "${RAG_MAX_SECONDS:-600}" \
                    -w "%{time_total},%{http_code}" \
                    -X POST "${JARVIS_BASE_URL}${path}" \
                    -b "${COOKIE_JAR}" \
                    -H "Content-Type: application/json" \
                    -H "Accept: application/json" \
                    -d "${body}" 2>/dev/null \
               || echo "ERR,000")
      echo "${scenario},${path},${result}"
    ) > "${TMPDIR_LG}/result_${i}.txt" &
  done
  wait
  cat "${TMPDIR_LG}"/result_*.txt > "${TMPDIR_LG}/results.txt" 2>/dev/null || true
}

# Collect a results.txt batch into the detail CSV + a latency file.
# Args: $1=latency_file
_drain_results() {
  local latfile="$1" row lat
  while IFS= read -r row; do
    echo "${row}" >> "${DETAIL_CSV}"
    lat=$(echo "${row}" | awk -F',' '{print $3}')
    [[ "${lat}" != "ERR" ]] && echo "${lat}" >> "${latfile}"
  done < "${TMPDIR_LG}/results.txt"
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
  # Percentile index (nearest-rank) — function must be at global scope for
  # GNU awk compatibility; it cannot be defined inside an END block.
  function pct(p,   idx) {
    idx = int(p/100 * n + 0.999999) - 1
    if (idx < 0) idx = 0
    if (idx >= n) idx = n-1
    return vals[idx]
  }
  BEGIN { n=0 }
  {
    vals[n] = $1
    n++
  }
  END {
    if (n == 0) {
      # NA (not 0) so a no-sample sweep is unambiguous downstream and does
      # not masquerade as p95=0.0 ("looks instant, actually all-timeout").
      print label ",0,NA,NA,NA,0"
      exit
    }
    p50 = pct(50)
    p95 = pct(95)
    p99 = pct(99)
    rps = (elapsed > 0) ? n / elapsed : 0
    printf "%s,%d,%.4f,%.4f,%.4f,%.2f\n", label, n, p50, p95, p99, rps
  }
  ' "${TMPDIR_LG}/sorted_${label}.txt"
}

_elapsed_seconds() {
  local start_ns="$1"
  local end_ns="$2"
  awk -v start="${start_ns}" -v end="${end_ns}" 'BEGIN {
    elapsed = (end - start) / 1000000000
    if (elapsed <= 0) elapsed = 0.001
    printf "%.6f", elapsed
  }'
}

_warm_get_path() {
  local path="$1"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
    "${JARVIS_BASE_URL}${path}" -b "${COOKIE_JAR}" -H "Accept: application/json" \
    2>/dev/null || echo "000")
  if ! [[ "${code}" =~ ^2[0-9][0-9]$ ]]; then
    log "WARN: warm-up GET failed for ${path} (HTTP ${code})"
    [[ "${LOADGEN_STRICT}" == "1" ]] && _strict_or_skip "warm-up GET failed for ${path}"
  fi
}

_warm_post_path() {
  local path="$1"
  local body="$2"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "${RAG_MAX_SECONDS:-600}" \
    -X POST "${JARVIS_BASE_URL}${path}" -b "${COOKIE_JAR}" \
    -H "Content-Type: application/json" -H "Accept: application/json" \
    -d "${body}" 2>/dev/null || echo "000")
  if ! [[ "${code}" =~ ^2[0-9][0-9]$ ]]; then
    log "WARN: warm-up POST failed for ${path} (HTTP ${code})"
    [[ "${LOADGEN_STRICT}" == "1" ]] && _strict_or_skip "warm-up POST failed for ${path}"
  fi
}

# Validate the completed fixed profile only in strict evidence mode. Any
# failure removes both CSVs so callers cannot mistake partial output for a
# successful measurement.
_validate_strict_evidence() {
  local scenario requests p50 p95 p99 rps extra
  local -A seen=()
  local required

  while IFS=, read -r scenario requests p50 p95 p99 rps extra; do
    [[ -z "${scenario}" ]] && continue
    if [[ -n "${extra}" ]] || ! _is_positive_integer "${requests}" ||
      (( requests < LOADGEN_MIN_SAMPLES )) || ! _is_decimal "${p50}" ||
      ! _is_decimal "${p95}" || ! _is_decimal "${p99}" || ! _is_decimal "${rps}"; then
      _strict_or_skip "invalid or undersampled summary row for ${scenario}"
    fi
    case "${scenario}" in
      scenario_a_fanout|scenario_b_pulse|scenario_c_search_hybrid|scenario_c_rag_ask) ;;
      *) _strict_or_skip "unexpected summary scenario ${scenario}" ;;
    esac
    if [[ -n "${seen[${scenario}]:-}" ]]; then
      _strict_or_skip "duplicate summary scenario ${scenario}"
    fi
    seen["${scenario}"]=1
  done < <(tail -n +2 "${SUMMARY_CSV}")

  for required in scenario_a_fanout scenario_b_pulse scenario_c_search_hybrid scenario_c_rag_ask; do
    [[ -n "${seen[${required}]:-}" ]] || _strict_or_skip "missing summary scenario ${required}"
  done

  while IFS=, read -r scenario _ seconds http_code extra; do
    if [[ -n "${extra}" ]] || [[ "${seconds}" == "ERR" ]] ||
      ! [[ "${http_code}" =~ ^2[0-9][0-9]$ ]]; then
      _strict_or_skip "failed request in ${scenario}"
    fi
  done < <(tail -n +2 "${DETAIL_CSV}")
}

# ---------------------------------------------------------------------------
# Write CSV headers
# ---------------------------------------------------------------------------
echo "scenario,endpoint,seconds,http_code" > "${DETAIL_CSV}"
echo "scenario,requests,p50_s,p95_s,p99_s,throughput_rps" > "${SUMMARY_CSV}"

# Verify the corpus before benchmark traffic can consume route budgets. The
# same snapshot is reported for Scenario C; retrieval still enforces current
# visibility on every measured request.
corpus_body=$(curl -fsS --max-time 10 -b "${COOKIE_JAR}" \
  "${JARVIS_BASE_URL}/api/papers/brief" 2>/dev/null || true)
corpus_n=$( { printf '%s' "${corpus_body}" | grep -o '"id"[[:space:]]*:[[:space:]]*[0-9]' || true; } \
  | wc -l | tr -d ' ')
if [[ "${corpus_n}" == "0" ]]; then
  log "WARN: empty corpus — pulse/RAG spans may be empty"
  _strict_or_skip "empty corpus — no embedded papers; scenario_c_rag_ask would be meaningless"
fi

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

for endpoint in "${A_ENDPOINTS[@]}"; do
  _warm_get_path "${endpoint}"
done
_warm_post_path "/api/papers/search-hybrid" \
  '{"query":"transformer attention mechanism","limit":10}'
_warm_post_path "/api/ask" \
  '{"question":"What methods and results are discussed across these papers?","decompose":true}'
if (( PERF_WARM_SETTLE_SECS > 0 )); then
  log "Warm-up complete; settling ${PERF_WARM_SETTLE_SECS}s before measurement"
  sleep "${PERF_WARM_SETTLE_SECS}"
fi
A_START="$(date +%s%N)"
: > "${TMPDIR_LG}/scen_a_latencies.txt"   # empty latency collector

for (( fanout_batch=1; fanout_batch<=LOADGEN_FANOUT_BATCHES; fanout_batch++ )); do
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
done

A_END="$(date +%s%N)"
A_ELAPSED="$(_elapsed_seconds "${A_START}" "${A_END}")"

_emit_stats "scenario_a_fanout" "${TMPDIR_LG}/scen_a_latencies.txt" "${A_ELAPSED}" \
  >> "${SUMMARY_CSV}"
log "Scenario A complete in ${A_ELAPSED}s"

# ---------------------------------------------------------------------------
# Scenario B — Pulse sustained-load
# Repeat concurrent batches against /health + /api/papers/feed for
# PERF_SUSTAIN_SECS seconds.  Simulates Pulse refresh background traffic.
# ---------------------------------------------------------------------------
if [[ "${LOADGEN_STRICT}" == "1" ]]; then
  log "Scenario B: fixed sustained-load (${LOADGEN_SUSTAIN_BATCHES} batch cycle(s), ${PERF_CONCURRENCY} concurrent)"
else
  log "Scenario B: Pulse sustained-load (${PERF_SUSTAIN_SECS}s window, ${PERF_CONCURRENCY} concurrent)"
fi
PULSE_ENDPOINTS=(
  "/health"
  "/api/papers/feed?limit=20"
)

: > "${TMPDIR_LG}/scen_b_latencies.txt"
B_START="$(date +%s%N)"
B_END_DEADLINE=$(( $(date +%s) + PERF_SUSTAIN_SECS ))

_run_sustain_cycle() {
  local endpoint row lat
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
}

if [[ "${LOADGEN_STRICT}" == "1" ]]; then
  for (( sustain_batch=1; sustain_batch<=LOADGEN_SUSTAIN_BATCHES; sustain_batch++ )); do
    _run_sustain_cycle
  done
else
  while [[ "$(date +%s)" -lt "${B_END_DEADLINE}" ]]; do
    _run_sustain_cycle
  done
fi

B_END="$(date +%s%N)"
B_ELAPSED="$(_elapsed_seconds "${B_START}" "${B_END}")"

_emit_stats "scenario_b_pulse" "${TMPDIR_LG}/scen_b_latencies.txt" "${B_ELAPSED}" \
  >> "${SUMMARY_CSV}"
log "Scenario B complete in ${B_ELAPSED}s"

# ---------------------------------------------------------------------------
# Scenario C — authenticated LLM hot-path drive
# Reuses the owner session established before Scenario A, then drives the four
# perf_probe.py sites so they emit spans. Best-effort mode retains its
# non-failing contract when the corpus is empty.
# ---------------------------------------------------------------------------
log "Scenario C: authenticated LLM hot-path drive"
  log "  corpus: ~${corpus_n} papers visible"

  # Developer profiling captures a Pulse trace alongside the hot paths. A
  # strict comparison excludes that asynchronous, rate-limited job so every
  # candidate measures the same fixed request population without background
  # model work contaminating one side of the comparison.
  if [[ "${LOADGEN_STRICT}" != "1" ]]; then
    pulse_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
                   -X POST "${JARVIS_BASE_URL}/api/pulse/generate" \
                   -b "${COOKIE_JAR}" -H "Content-Type: application/json" \
                   -d '{}' 2>/dev/null || echo "000")
    log "  POST /api/pulse/generate → HTTP ${pulse_code} (async; stage-2 runs in worker)"
  fi

  # Batch 1 — hybrid search: embed_texts_post + hybrid_search_bm25_sql
  : > "${TMPDIR_LG}/scen_c_search_lat.txt"
  C1_START="$(date +%s%N)"
  _search_b=1
  while [[ ${_search_b} -le ${SEARCH_BATCHES} ]]; do
    _fire_post_batch "scenario_c_search" "/api/papers/search-hybrid" \
      '{"query":"transformer attention mechanism","limit":10}' "${PERF_CONCURRENCY}"
    _drain_results "${TMPDIR_LG}/scen_c_search_lat.txt"
    _search_b=$(( _search_b + 1 ))
  done
  C1_ELAPSED="$(_elapsed_seconds "${C1_START}" "$(date +%s%N)")"
  _emit_stats "scenario_c_search_hybrid" "${TMPDIR_LG}/scen_c_search_lat.txt" "${C1_ELAPSED}" \
    >> "${SUMMARY_CSV}"
  log "  search-hybrid ×${PERF_CONCURRENCY}×${SEARCH_BATCHES} batches done in ${C1_ELAPSED}s"

  # Batch 2 — cross-paper RAG: prepare_cross_paper_rag + embed_texts_post.
  # RAG_BATCHES back-to-back batches accumulate into one latency file so p95
  # is over n = RAG_BATCHES×RAG_CONCURRENCY (default 1 = legacy n=conc).
  : > "${TMPDIR_LG}/scen_c_rag_lat.txt"
  C2_START="$(date +%s%N)"
  _rag_b=1
  while [[ ${_rag_b} -le ${RAG_BATCHES} ]]; do
    _fire_post_batch "scenario_c_rag" "/api/ask" \
      '{"question":"What methods and results are discussed across these papers?","decompose":true}' \
      "${RAG_CONCURRENCY}"
    _drain_results "${TMPDIR_LG}/scen_c_rag_lat.txt"
    _rag_b=$(( _rag_b + 1 ))
  done
  C2_ELAPSED="$(_elapsed_seconds "${C2_START}" "$(date +%s%N)")"
  _emit_stats "scenario_c_rag_ask" "${TMPDIR_LG}/scen_c_rag_lat.txt" "${C2_ELAPSED}" \
    >> "${SUMMARY_CSV}"
  log "  /api/ask ×${RAG_CONCURRENCY}×${RAG_BATCHES} batches (decompose) done in ${C2_ELAPSED}s"

  # Give the async Pulse worker a window to emit pulse_stage2_llm before
  # profile.sh collects the probe JSONL. Best-effort, bounded.
  settle="${PERF_PULSE_SETTLE_SECS:-25}"
  log "  settling ${settle}s for the async Pulse stage-2 worker span"
  sleep "${settle}"

if [[ "${LOADGEN_STRICT}" == "1" ]]; then
  _validate_strict_evidence
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "Load-gen artifacts:"
log "  detail  → ${DETAIL_CSV}"
log "  summary → ${SUMMARY_CSV}"
