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
#     Bootstraps a real owner session via POST /api/auth/api-key-session
#     (exchanges the existing JARVIS_API_KEY for a jarvis_session cookie —
#     no email/magic-link needed), then drives the four flag-gated
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
#   RAG_CONCURRENCY        Concurrency for the heavy Scenario C /api/ask
#                          (cross-paper RAG) batch.  Default: min(PERF_CONCURRENCY,3)
#                          — RAG is LLM-bound; a high fan-out just queues on the
#                          single shared ollama and skews latency.
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

# RAG_BATCHES: how many back-to-back batches of RAG_CONCURRENCY /api/ask
# requests to fire for scenario_c_rag. Default 1 = EXACT legacy behaviour
# (n = RAG_CONCURRENCY → make profile unchanged). The confirmatory bench
# sets this >1 so scenario_c_rag_ask p95 is computed over n =
# RAG_BATCHES×RAG_CONCURRENCY samples instead of a statistically
# meaningless "max of 4/8".
RAG_BATCHES="${RAG_BATCHES:-1}"

# _strict_or_skip <reason> : in strict mode, record + exit 3; else return 0
# so the caller's legacy "log WARN + exit 0" path runs unchanged.
_strict_or_skip() {
  if [[ "${LOADGEN_STRICT}" == "1" ]]; then
    echo "loadgen FATAL: $* at $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >> "${OUT_DIR}/loadgen-FATAL.txt"
    exit 3
  fi
  return 0
}

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
# Reachability guard — degrade gracefully when the server is not running.
# Probe /health/live, NOT /health: /health does heavy dependency fan-out
# (LiteLLM→LLM, Ollama, Qdrant) that routinely exceeds a 2s budget under load
# or while a model is warming, yielding a FALSE "unreachable" that aborts the
# whole run before any stats are emitted (empty loadgen-summary.csv → NA
# metrics). /health/live is the purpose-built, rate-limiter-exempt liveness
# route (added 2026-05-17) — it answers in milliseconds regardless of deps.
# Timeout raised to 10s for slow-boot / GPU-saturated boxes.
# ---------------------------------------------------------------------------
if ! curl -s -o /dev/null --max-time 10 "${PAPER_INGEST_BASE}/health/live" \
     -H "X-API-Key: ${API_KEY}" -H "Accept: application/json" 2>/dev/null; then
  log "WARN: backend not reachable at ${PAPER_INGEST_BASE} — skipping loadgen scenarios"
  log "      Start the stack with 'make up' and re-run to collect concurrency metrics."
  _strict_or_skip "backend not reachable at ${PAPER_INGEST_BASE}/health/live"
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
                    -X POST "${PAPER_INGEST_BASE}${path}" \
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
# Scenario C — authenticated LLM hot-path drive
# Mints a real owner session from the API key, then drives the four
# perf_probe.py sites so they emit spans.  Best-effort: any precondition
# failure logs a warning and skips (exit 0 contract preserved).
# ---------------------------------------------------------------------------
COOKIE_JAR="${TMPDIR_LG}/jarvis_session.cookies"

log "Scenario C: authenticated LLM hot-path drive"
auth_code=$(curl -s -o "${TMPDIR_LG}/auth_resp.json" -w '%{http_code}' \
              --max-time 15 -X POST "${PAPER_INGEST_BASE}/api/auth/api-key-session" \
              -H "X-API-Key: ${API_KEY}" -H "Content-Type: application/json" \
              -c "${COOKIE_JAR}" -d '{}' 2>/dev/null || echo "000")

if [[ "${auth_code}" != "200" ]] || ! grep -q 'jarvis_session' "${COOKIE_JAR}" 2>/dev/null; then
  log "WARN: api-key-session → HTTP ${auth_code} (need single-tenant + an admin"
  log "      user, or API_KEY_LOGIN_ENABLED). Skipping Scenario C — the LLM"
  log "      hot-path probes will not emit this run."
  _strict_or_skip "Scenario C session mint failed (api-key-session HTTP ${auth_code}) — no scenario_c_rag_ask possible"
else
  log "  session minted (owner: $(grep -o '\"email\":\"[^\"]*\"' "${TMPDIR_LG}/auth_resp.json" 2>/dev/null | head -1))"

  # Corpus presence — informational; pulse/RAG spans need ≥1 embedded paper.
  corpus_n=$(curl -s --max-time 10 -b "${COOKIE_JAR}" \
               "${PAPER_INGEST_BASE}/api/papers/brief" 2>/dev/null \
             | grep -o '"id"' | wc -l | tr -d ' ' || echo 0)
  log "  corpus: ~${corpus_n} papers visible"
  if [[ "${corpus_n}" == "0" ]]; then
    log "  WARN: empty corpus — pulse/RAG spans may be empty (query-embed + BM25 still fire)"
    _strict_or_skip "empty corpus — no embedded papers; scenario_c_rag_ask would be meaningless"
  fi

  # Fire Pulse FIRST: it's an async deferred job (rate-limit 3/hour). Firing
  # early lets the worker run pulse_stage2_llm concurrently with the search/RAG
  # batches below so its span lands before profile.sh collects the JSONL.
  pulse_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
                 -X POST "${PAPER_INGEST_BASE}/api/pulse/generate" \
                 -b "${COOKIE_JAR}" -H "Content-Type: application/json" \
                 -d '{}' 2>/dev/null || echo "000")
  echo "scenario_c_pulse,/api/pulse/generate,0,${pulse_code}" >> "${DETAIL_CSV}"
  log "  POST /api/pulse/generate → HTTP ${pulse_code} (async; stage-2 runs in worker)"

  # Batch 1 — hybrid search: embed_texts_post + hybrid_search_bm25_sql
  : > "${TMPDIR_LG}/scen_c_search_lat.txt"
  C1_START="$(date +%s)"
  _fire_post_batch "scenario_c_search" "/api/papers/search-hybrid" \
    '{"query":"transformer attention mechanism","limit":10}' "${PERF_CONCURRENCY}"
  _drain_results "${TMPDIR_LG}/scen_c_search_lat.txt"
  C1_ELAPSED=$(( $(date +%s) - C1_START )); [[ ${C1_ELAPSED} -le 0 ]] && C1_ELAPSED=1
  _emit_stats "scenario_c_search_hybrid" "${TMPDIR_LG}/scen_c_search_lat.txt" "${C1_ELAPSED}" \
    >> "${SUMMARY_CSV}"
  log "  search-hybrid ×${PERF_CONCURRENCY} done in ${C1_ELAPSED}s"

  # Batch 2 — cross-paper RAG: prepare_cross_paper_rag + embed_texts_post.
  # RAG_BATCHES back-to-back batches accumulate into one latency file so p95
  # is over n = RAG_BATCHES×RAG_CONCURRENCY (default 1 = legacy n=conc).
  : > "${TMPDIR_LG}/scen_c_rag_lat.txt"
  C2_START="$(date +%s)"
  _rag_b=1
  while [[ ${_rag_b} -le ${RAG_BATCHES} ]]; do
    _fire_post_batch "scenario_c_rag" "/api/ask" \
      '{"question":"What methods and results are discussed across these papers?","decompose":true}' \
      "${RAG_CONCURRENCY}"
    _drain_results "${TMPDIR_LG}/scen_c_rag_lat.txt"
    _rag_b=$(( _rag_b + 1 ))
  done
  C2_ELAPSED=$(( $(date +%s) - C2_START )); [[ ${C2_ELAPSED} -le 0 ]] && C2_ELAPSED=1
  _emit_stats "scenario_c_rag_ask" "${TMPDIR_LG}/scen_c_rag_lat.txt" "${C2_ELAPSED}" \
    >> "${SUMMARY_CSV}"
  log "  /api/ask ×${RAG_CONCURRENCY}×${RAG_BATCHES} batches (decompose) done in ${C2_ELAPSED}s"

  # Give the async Pulse worker a window to emit pulse_stage2_llm before
  # profile.sh collects the probe JSONL. Best-effort, bounded.
  settle="${PERF_PULSE_SETTLE_SECS:-25}"
  log "  settling ${settle}s for the async Pulse stage-2 worker span"
  sleep "${settle}"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "Load-gen artifacts:"
log "  detail  → ${DETAIL_CSV}"
log "  summary → ${SUMMARY_CSV}"
