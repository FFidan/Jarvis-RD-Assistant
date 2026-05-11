#!/usr/bin/env bash
# scripts/profile.sh — Bucket G perf profiling harness.
#
# Captures a baseline snapshot of frontend bundle sizes, backend endpoint
# wall-clock timings, and (optionally) py-spy flamegraph + pg_stat_statements
# top-N when those tools are available.
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
  PAPER_PID=$(docker top jarvis_rd_assistant-paper_ingestion-1 2>/dev/null \
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
PG_CONTAINER="jarvis_rd_assistant-postgres-1"
if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
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

log "Profiling complete. Artifacts in ${OUT_DIR}"
