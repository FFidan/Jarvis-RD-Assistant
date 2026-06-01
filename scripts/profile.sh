#!/usr/bin/env bash
# scripts/profile.sh — perf profiling harness.
#
# Captures a baseline snapshot of frontend bundle sizes, backend endpoint
# wall-clock timings, and (optionally) py-spy flamegraph + pg_stat_statements
# top-N when those tools are available.
#
# Also runs concurrency load generation (scripts/perf/loadgen.sh) and GPU/VRAM
# telemetry (scripts/perf/gpu_probe.sh) as additive, best-effort captures.
#
# Output: artifacts/perf/<UTC-timestamp>/
#
# Pre-reqs (best-effort — script degrades gracefully if missing):
#   - Docker stack already up (`make up`)
#   - frontend/node_modules installed (`cd frontend && npm install`)
#   - py-spy installed (`pipx install py-spy` or `uv tool install py-spy`)
#     and the running paper_ingestion uvicorn PID accessible to attach
#     (typically requires sudo on Linux unless cap_sys_ptrace is granted).
#   - pg_stat_statements extension preloaded in Postgres
#     (set `shared_preload_libraries = 'pg_stat_statements'` in postgresql.conf
#      and `CREATE EXTENSION pg_stat_statements;`).
#   - nvidia-smi (for GPU telemetry; degrades gracefully when absent)
#
# Environment knobs (all optional):
#   PERF_CONCURRENCY        Concurrent workers for loadgen (default: 10)
#   PERF_PROBE_ENABLED      Set to 1 to enable in-process perf probes during
#                           the load window; probes write JSONL to
#                           ${OUT_DIR}/perf-probe.jsonl (default: 0)
#   PERF_PROBE_PATH         Override probe output path (default: auto-set to
#                           ${OUT_DIR}/perf-probe.jsonl)
#   PERF_GPU_POLL_SECONDS   gpu_probe.sh polling interval in seconds (default: 2)
#   SKIP_LIGHTHOUSE         Set to 1 to skip Lighthouse (useful on CI; default: 0)
#   PAPER_INGESTION_HOST_PORT  Backend port (default: 8010)
#
# This script never fails the build — missing tools log warnings instead.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${REPO_ROOT}/artifacts/perf/${TIMESTAMP}"
mkdir -p "${OUT_DIR}"

API_KEY_FILE="${REPO_ROOT}/secrets/jarvis_api_key.txt"
PAPER_INGEST_PORT="${PAPER_INGESTION_HOST_PORT:-8010}"
PAPER_INGEST_BASE="http://localhost:${PAPER_INGEST_PORT}"

log() { echo "[profile] $*" >&2; }

API_KEY=""
if [[ -f "${API_KEY_FILE}" ]]; then
  API_KEY="$(cat "${API_KEY_FILE}")"
fi

# In-process perf probes (perf_probe.py) run INSIDE the paper_ingestion
# container and write to /data/perf/perf-probe.jsonl, which the perf compose
# override bind-mounts here. Ensure the dir exists and is container-writable,
# and truncate any prior run's spans so this artifact reflects only this run.
PROBE_HOST_DIR="${REPO_ROOT}/shared/perf"
PROBE_HOST_FILE="${PROBE_HOST_DIR}/perf-probe.jsonl"
mkdir -p "${PROBE_HOST_DIR}"
chmod 777 "${PROBE_HOST_DIR}" 2>/dev/null || true
: > "${PROBE_HOST_FILE}" 2>/dev/null || true
chmod 666 "${PROBE_HOST_FILE}" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 0. GPU probe — start in background (best-effort)
# ---------------------------------------------------------------------------
GPU_PROBE_SCRIPT="${REPO_ROOT}/scripts/perf/gpu_probe.sh"
GPU_PROBE_SENTINEL="${OUT_DIR}/.gpu_probe.stop"
GPU_PROBE_PID=""

if [[ -f "${GPU_PROBE_SCRIPT}" ]]; then
  log "Starting GPU probe in background"
  OUT_DIR="${OUT_DIR}" \
  PERF_GPU_PROBE_STOP="${GPU_PROBE_SENTINEL}" \
  PERF_CONCURRENCY="${PERF_CONCURRENCY:-10}" \
  PERF_GPU_POLL_SECONDS="${PERF_GPU_POLL_SECONDS:-2}" \
  bash "${GPU_PROBE_SCRIPT}" &
  GPU_PROBE_PID="$!"
  log "GPU probe PID ${GPU_PROBE_PID} (sentinel: ${GPU_PROBE_SENTINEL})"
else
  log "WARN: ${GPU_PROBE_SCRIPT} not found — skipping GPU telemetry"
fi

# ---------------------------------------------------------------------------
# 1. Frontend bundle baseline
# ---------------------------------------------------------------------------
log "Building frontend bundle"
if (cd "${REPO_ROOT}/frontend" && npm run build > "${OUT_DIR}/frontend-build.log" 2>&1); then
  ls -lh "${REPO_ROOT}/frontend/dist/assets/"*.js | sort -k5 -h \
    > "${OUT_DIR}/frontend-bundle-sizes.txt"
  log "Frontend bundle sizes saved → ${OUT_DIR}/frontend-bundle-sizes.txt"
else
  log "WARN: frontend build failed; see ${OUT_DIR}/frontend-build.log"
fi

# ---------------------------------------------------------------------------
# 2. Backend endpoint wall-clock timings
# ---------------------------------------------------------------------------
log "Timing backend GET endpoints (3x each)"
{
  echo "endpoint,run,seconds,http_code"
  for path in \
    "/api/papers/feed?limit=20" \
    "/api/papers/brief" \
    "/api/dashboard/metrics" \
    "/api/system/models" \
    "/health"
  do
    for i in 1 2 3; do
      t=$(curl -s -o /dev/null \
            -w "%{time_total},%{http_code}" \
            "${PAPER_INGEST_BASE}${path}" \
            -H "X-API-Key: ${API_KEY}" \
            -H "Accept: application/json" 2>/dev/null || echo "ERR,000")
      echo "${path},${i},${t}"
    done
  done
} > "${OUT_DIR}/backend-timings.csv"
log "Backend timings saved → ${OUT_DIR}/backend-timings.csv"

# ---------------------------------------------------------------------------
# 3. py-spy flamegraph (best-effort)
# ---------------------------------------------------------------------------
if command -v py-spy >/dev/null 2>&1; then
  PAPER_PID=$(docker top "$(docker compose ps -q paper_ingestion 2>/dev/null | head -n 1)" 2>/dev/null \
    | awk 'NR==2 {print $2}')
  if [[ -n "${PAPER_PID:-}" ]]; then
    log "Recording py-spy flamegraph (30s) on PID ${PAPER_PID}"
    if py-spy record \
        --pid "${PAPER_PID}" \
        --duration 30 \
        --output "${OUT_DIR}/flamegraph.svg" \
        2> "${OUT_DIR}/py-spy.log"; then
      log "Flamegraph saved → ${OUT_DIR}/flamegraph.svg"
    else
      log "WARN: py-spy record failed (likely needs sudo); see py-spy.log"
    fi
  else
    log "WARN: paper_ingestion container not running — skipping py-spy"
  fi
else
  log "WARN: py-spy not installed — skipping flamegraph"
fi

# ---------------------------------------------------------------------------
# 4. pg_stat_statements top-N (best-effort)
# ---------------------------------------------------------------------------
PG_CONTAINER="$(docker compose ps -q postgres 2>/dev/null | head -n 1)"
if [[ -n "$PG_CONTAINER" ]]; then
  if docker exec "${PG_CONTAINER}" psql -U jarvis -d jarvis -tAc \
       "SELECT count(*) FROM pg_stat_statements;" >/dev/null 2>&1; then
    log "Dumping pg_stat_statements top-20 by total_exec_time"
    docker exec "${PG_CONTAINER}" psql -U jarvis -d jarvis -F',' --csv -c \
      "SELECT calls, round(total_exec_time::numeric, 2) AS total_ms,
              round(mean_exec_time::numeric, 2) AS mean_ms,
              round((100 * total_exec_time /
                     NULLIF(sum(total_exec_time) OVER (), 0))::numeric, 2)
                AS pct_total,
              substring(query, 1, 200) AS query
         FROM pg_stat_statements
         ORDER BY total_exec_time DESC
         LIMIT 20;" \
      > "${OUT_DIR}/pg-stat-statements-top20.csv" 2>&1
    log "pg_stat_statements saved → ${OUT_DIR}/pg-stat-statements-top20.csv"
  else
    log "WARN: pg_stat_statements not preloaded — see HOWTO.md"
  fi
else
  log "WARN: Postgres container not running — skipping query log"
fi

# ---------------------------------------------------------------------------
# 5. Lighthouse (best-effort)
# ---------------------------------------------------------------------------
if command -v npx >/dev/null 2>&1 && [[ "${SKIP_LIGHTHOUSE:-}" != "1" ]]; then
  log "Running Lighthouse on http://localhost:3001 (best-effort, may skip)"
  if npx --yes lighthouse http://localhost:3001 \
        --quiet \
        --output html \
        --output-path "${OUT_DIR}/lighthouse.html" \
        --chrome-flags="--headless --no-sandbox" \
        > "${OUT_DIR}/lighthouse.log" 2>&1; then
    log "Lighthouse report → ${OUT_DIR}/lighthouse.html"
  else
    log "WARN: Lighthouse run failed (chrome unavailable?); see lighthouse.log"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Concurrency load generation (best-effort)
#    Runs AFTER sequential captures so perf-probe.jsonl reflects real load.
#    PERF_PROBE_ENABLED=1 + PERF_PROBE_PATH activate in-process span probes.
# ---------------------------------------------------------------------------
LOADGEN_SCRIPT="${REPO_ROOT}/scripts/perf/loadgen.sh"

if [[ -f "${LOADGEN_SCRIPT}" ]]; then
  log "Running load-gen (PERF_CONCURRENCY=${PERF_CONCURRENCY:-10})"
  OUT_DIR="${OUT_DIR}" \
  PERF_CONCURRENCY="${PERF_CONCURRENCY:-10}" \
  PERF_PROBE_ENABLED="${PERF_PROBE_ENABLED:-0}" \
  PERF_PROBE_PATH="${PERF_PROBE_PATH:-${OUT_DIR}/perf-probe.jsonl}" \
  PAPER_INGESTION_HOST_PORT="${PAPER_INGESTION_HOST_PORT:-8010}" \
  bash "${LOADGEN_SCRIPT}" || log "WARN: loadgen exited non-zero — concurrency metrics may be partial"
else
  log "WARN: ${LOADGEN_SCRIPT} not found — skipping concurrency load generation"
fi

# ---------------------------------------------------------------------------
# 7. Stop GPU probe — sentinel file first, then SIGTERM fallback
# ---------------------------------------------------------------------------
if [[ -n "${GPU_PROBE_PID}" ]]; then
  log "Stopping GPU probe (PID ${GPU_PROBE_PID})"
  # Sentinel lets the probe exit cleanly from its sleep/check loop.
  touch "${GPU_PROBE_SENTINEL}" 2>/dev/null || true
  # Give it up to 10 seconds to stop on its own; SIGTERM if not done.
  local_wait=0
  while kill -0 "${GPU_PROBE_PID}" 2>/dev/null && [[ "${local_wait}" -lt 10 ]]; do
    sleep 1
    (( local_wait++ )) || true
  done
  if kill -0 "${GPU_PROBE_PID}" 2>/dev/null; then
    log "GPU probe still alive after ${local_wait}s — sending SIGTERM"
    kill -TERM "${GPU_PROBE_PID}" 2>/dev/null || true
    wait "${GPU_PROBE_PID}" 2>/dev/null || true
  else
    wait "${GPU_PROBE_PID}" 2>/dev/null || true
  fi
  rm -f "${GPU_PROBE_SENTINEL}"
  log "GPU probe stopped. Timeseries → ${OUT_DIR}/gpu-timeseries.jsonl"
  log "Run metadata → ${OUT_DIR}/run-metadata.json"
fi

# ---------------------------------------------------------------------------
# 8. Collect in-container perf-probe spans into the artifact dir
# ---------------------------------------------------------------------------
if [[ -s "${PROBE_HOST_FILE}" ]]; then
  cp "${PROBE_HOST_FILE}" "${OUT_DIR}/perf-probe.jsonl"
  probe_lines="$(wc -l < "${OUT_DIR}/perf-probe.jsonl" | tr -d ' ')"
  log "Collected ${probe_lines} perf-probe span(s) → ${OUT_DIR}/perf-probe.jsonl"
else
  log "WARN: ${PROBE_HOST_FILE} empty — no in-process probe spans captured."
  log "      Confirm the stack was booted via 'make profile-stack-up' (perf"
  log "      override sets PERF_PROBE_ENABLED=1 + the /data/perf bind-mount)."
fi

log "Profiling complete. Artifacts in ${OUT_DIR}"
