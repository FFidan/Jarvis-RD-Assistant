#!/usr/bin/env bash
# scripts/perf/vllm_confirmatory_bench.sh
# =============================================================================
# Portable, hermetic vLLM-vs-Ollama CONFIRMATORY bench.
#
# Runs the entire matched-pair production-size bench end-to-end on a box the
# agent cannot see, and emits ONE results bundle the agent can verdict from
# with zero box access. This is the pre-registered confirmatory run that gates
# vLLM adoption (the 0.5B lean bench PASSED but was deliberately not adopted).
#
# Plan of record: ~/.claude/plans/handoff-audit-high-cleanup-playful-bengio.md
# Draft/context : docs/perf/2026-05-18-vllm-confirmatory-bench-plan.md
#
# CONTRACT — differs from loadgen.sh/profile.sh (which degrade to exit 0):
#   Any precondition failure HARD-ABORTS (non-zero) but the EXIT trap STILL
#   emits a partial+diagnostic bundle. Never a silent meaningless result.
#
# Matrix: BENCH_PAIRS × {vllm,ollama} × BENCH_CONCURRENCY  (default 2×2×2 = 8
# measured sweeps) + 1 SSE quality capture per engine per pair.
#
#   Pair A : vLLM Qwen/Qwen2.5-7B-Instruct-AWQ  vs Ollama qwen2.5:7b-instruct
#   Pair B : vLLM Qwen/Qwen3-8B-AWQ             vs Ollama qwen3:8b
#            (fallback recorded in the bundle if no clean Qwen3-8B AWQ)
#
# Adoption gate (computed into c1_c2_c3.json — NO soak, dropped by decision):
#   C1 = scenario_c_rag_ask p95 improvement ≥ 30% at concurrency ≥ 4
#   C2 = embed_texts_post p95 drift < 10%
#   + manual answer-quality sign-off from quality/<pair>/DIFF.md
#
# Self-test on a 16 GB box (proves the harness, not the verdict):
#   BENCH_PAIRS=A BENCH_CONCURRENCY=4 MIN_VRAM_MB=0 \
#   PAIR_A_VLLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct \
#   PAIR_A_OLLAMA_MODEL=qwen2.5:0.5b-instruct \
#   VLLM_GPU_MEMORY_UTILIZATION=0.30 \
#   bash scripts/perf/vllm_confirmatory_bench.sh
# =============================================================================
set -uo pipefail   # NOT -e: we control aborts so the trap always bundles.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export BENCH_REPO_ROOT="${REPO_ROOT}"
# shellcheck source=scripts/perf/_bench_lib.sh
source "${REPO_ROOT}/scripts/perf/_bench_lib.sh"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${REPO_ROOT}/artifacts/perf/vllm-confirmatory-${TS}"
export BENCH_OUT_DIR="${OUT_DIR}"
mkdir -p "${OUT_DIR}"

# --- knobs (defaults = the real 48 GB matrix; self-test overrides via env) --
BENCH_PAIRS="${BENCH_PAIRS:-A B}"
BENCH_CONCURRENCY="${BENCH_CONCURRENCY:-4 8}"
MIN_VRAM_MB="${MIN_VRAM_MB:-40000}"
BENCH_REBUILD="${BENCH_REBUILD:-1}"
BENCH_SEED_QUERY="${BENCH_SEED_QUERY:-cat:cs.CL AND abs:transformer}"
BENCH_SEED_MAX="${BENCH_SEED_MAX:-5}"

PAIR_A_VLLM_MODEL="${PAIR_A_VLLM_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"
PAIR_A_OLLAMA_MODEL="${PAIR_A_OLLAMA_MODEL:-qwen2.5:7b-instruct}"
PAIR_B_VLLM_MODEL="${PAIR_B_VLLM_MODEL:-Qwen/Qwen3-8B-AWQ}"
PAIR_B_OLLAMA_MODEL="${PAIR_B_OLLAMA_MODEL:-qwen3:8b}"

export VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.6.6}"   # sm_89 OK (RTX Ada)
export VLLM_HOST_PORT="${VLLM_HOST_PORT:-8080}"
export VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.90}"
export VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"

PORT="${PAPER_INGESTION_HOST_PORT:-8010}"
BASE="http://localhost:${PORT}"
OLLAMA_BASE="http://localhost:${OLLAMA_HOST_PORT:-11434}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vllm.yml"
API_KEY_FILE="${REPO_ROOT}/secrets/jarvis_api_key.txt"
COOKIE_JAR="${OUT_DIR}/.jarvis_session.cookies"

ABORTED=0

# --- EXIT trap: ALWAYS restore + bundle, even on abort --------------------
finalize() {
  local rc=$?
  bench_log "finalize (rc=${rc}) — restoring + bundling"
  litellm_restore || bench_warn "litellm restore failed — inspect git status"
  ( cd "${REPO_ROOT}" && ${COMPOSE} --profile vllm stop vllm >/dev/null 2>&1 ) || true
  write_results_md "${rc}"
  ( cd "$(dirname "${OUT_DIR}")" && tar czf "${OUT_DIR}.tar.gz" "$(basename "${OUT_DIR}")" ) \
    && bench_log "BUNDLE → ${OUT_DIR}.tar.gz"
  if [[ "${ABORTED}" -eq 1 || ${rc} -ne 0 ]]; then
    bench_warn "Run ended ABORTED/non-zero — bundle is partial+diagnostic."
  fi
}
trap finalize EXIT

die() { ABORTED=1; bench_die "$@"; exit 1; }
run_or_die() { "$@" || die "command failed: $*"; }

# =============================================================================
# Stage 1 — Preflight (hard aborts, but bundle still emitted)
# =============================================================================
stage_preflight() {
  bench_log "Stage 1: preflight"
  local missing=()
  for t in docker curl python3 git awk tar; do
    command -v "$t" >/dev/null 2>&1 || missing+=("$t")
  done
  [[ ${#missing[@]} -eq 0 ]] || die "missing required tools: ${missing[*]}"

  if command -v nvidia-smi >/dev/null 2>&1; then
    local vram
    vram="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
    echo "gpu_vram_total_mb=${vram}" >> "${OUT_DIR}/env.txt"
    if [[ "${vram}" =~ ^[0-9]+$ ]] && (( vram < MIN_VRAM_MB )); then
      die "VRAM ${vram}MB < required ${MIN_VRAM_MB}MB (wrong box? set MIN_VRAM_MB=0 only for harness self-test)"
    fi
  else
    die "nvidia-smi absent — cannot confirm GPU box"
  fi

  # RAG-DB-1 fix MUST be in the working tree, else /api/ask 500s → bench void.
  local rag="${REPO_ROOT}/services/paper_ingestion/paper_ingestion/rag/streaming.py"
  if ! grep -q 'EXISTS (SELECT 1 FROM user_library' "${rag}" 2>/dev/null; then
    die "RAG-DB-1 fix (commit 78cdaf1a) absent in rag/streaming.py — /api/ask would 500. Pull the fix first."
  fi
  if grep -Eq 'papers\.user_id|p\.user_id|AND \(user_id = \$2 OR user_id IS NULL\)' "${rag}" 2>/dev/null; then
    die "phantom papers.user_id predicate still present in rag/streaming.py — wrong/old code"
  fi

  for f in "${API_KEY_FILE}" \
           "${REPO_ROOT}/docker-compose.vllm.yml" \
           "${REPO_ROOT}/docker-compose.perf.yml" \
           "${REPO_ROOT}/litellm/config.yaml"; do
    [[ -f "$f" ]] || die "required file missing: $f"
  done
  grep -q '^profile-stack-up:' "${REPO_ROOT}/Makefile" || die "Makefile target profile-stack-up missing"

  # Embed-dimension drift guard. The app validates returned vectors against
  # EMBEDDING_DIMENSION (.env), while embeddings are served by the LiteLLM
  # `embed` alias whose `dimensions:` is pinned in litellm/config.yaml. A box
  # whose .env predates the qwen3-embedding migration (nomic 768) against the
  # current alias (qwen3-embedding:4b 2560) makes Stage 4 ALWAYS abort with
  # "Embedding dimension mismatch". Catch it here, not after GPU hours.
  local env_file="${REPO_ROOT}/.env"
  if [[ -f "${env_file}" ]]; then
    local env_dim alias_dim
    env_dim="$(grep -E '^EMBEDDING_DIMENSION=' "${env_file}" | tail -1 | cut -d= -f2 | tr -d ' "')"
    # dimensions: under the exact `model_name: "embed"` block (not embed-4b).
    # Skip comment lines: an example `# - model_name: "embed"` block precedes
    # the live alias and would otherwise poison the match.
    alias_dim="$(awk '
      /^[[:space:]]*#/ {next}
      /model_name:[[:space:]]*"embed"[[:space:]]*$/ {inblk=1; next}
      inblk && /- model_name:/ {inblk=0}
      inblk && /dimensions:/ {
        v=$0; sub(/.*dimensions:[[:space:]]*/,"",v); sub(/[^0-9].*/,"",v)
        print v; exit
      }
    ' "${REPO_ROOT}/litellm/config.yaml")"
    if [[ -n "${env_dim}" && -n "${alias_dim}" && "${env_dim}" != "${alias_dim}" ]]; then
      die "embed-dimension drift: .env EMBEDDING_DIMENSION=${env_dim} but litellm/config.yaml 'embed' alias serves ${alias_dim}-d. Stage 4 would abort on every paper. Fix: set EMBEDDING_DIMENSION=${alias_dim} (and EMBEDDING_MODEL_NAME to match the alias model) in ${env_file}, then recreate paper_ingestion."
    fi
    echo "embed_dim_env=${env_dim:-unset} embed_dim_alias=${alias_dim:-unparsed}" >> "${OUT_DIR}/env.txt"
  fi

  { echo "ts_utc=${TS}"
    echo "git_head=$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null)"
    echo "git_status_dirty=$(git -C "${REPO_ROOT}" status --porcelain | wc -l | tr -d ' ')"
    echo "vllm_image=${VLLM_IMAGE}"
    echo "pairs=${BENCH_PAIRS} concurrency=${BENCH_CONCURRENCY}"
  } >> "${OUT_DIR}/env.txt"
  bench_log "preflight OK"
}

# =============================================================================
# Stage 2 — Mandatory image rebuild at HEAD (stale image → empty probes)
# =============================================================================
stage_rebuild() {
  if [[ "${BENCH_REBUILD}" != "1" ]]; then
    bench_warn "BENCH_REBUILD=0 — SKIPPING rebuild (self-test only; recorded)"
    echo "rebuild=SKIPPED" >> "${OUT_DIR}/env.txt"
    return 0
  fi
  bench_log "Stage 2: rebuild paper_ingestion/learning_engine/dashboard at HEAD"
  ( cd "${REPO_ROOT}" && docker compose -f docker-compose.yml build \
      paper_ingestion learning_engine dashboard ) \
    || die "image rebuild failed"
  echo "rebuild=DONE" >> "${OUT_DIR}/env.txt"
}

# =============================================================================
# Stage 3 — Boot stack with probes armed
# =============================================================================
stage_boot() {
  bench_log "Stage 3: make profile-stack-up (probes armed)"
  # paper_ingestion calls ensure_collection() on qdrant during lifespan, and
  # sweep stages route through litellm+ollama. profile-stack-up uses --no-deps
  # so those services are never started by make. Pre-start them here on the
  # same compose overlay so the network exists before make runs and paper_ingestion
  # finds qdrant available on first connection attempt.
  # Qdrant carries a dimension-pinned collection (paper_chunks). A prior run —
  # or a box whose .env embedding model has since changed — leaves it at the
  # old dimension; ensure_collection() then HARD-FAILS paper_ingestion startup
  # ("collection has dimension N; expected M"). The corpus is deterministically
  # re-seeded in Stage 4 every run, so qdrant state is fully disposable. Wipe
  # the volume so the collection is always recreated at the currently
  # configured EMBEDDING_DIMENSION. Idempotent; runs before the pre-start.
  local qd_vol
  qd_vol="$(docker inspect jarvis-rd-assistant-qdrant-1 \
    --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Name}}{{end}}{{end}}' \
    2>/dev/null || true)"
  [[ -n "${qd_vol}" ]] || qd_vol="jarvis-rd-assistant_qdrant_data"
  ( cd "${REPO_ROOT}" && \
    LETSENCRYPT_DOMAIN=local LETSENCRYPT_EMAIL=local@local.dev \
    docker compose --env-file .env --env-file versions.env \
      -f docker-compose.yml -f docker-compose.perf.yml --profile perf \
      rm -sf qdrant ) >/dev/null 2>&1 || true
  docker volume rm "${qd_vol}" >/dev/null 2>&1 || true
  bench_log "  qdrant volume reset (${qd_vol}) — collection recreated at current dim"

  bench_log "  pre-starting qdrant ollama litellm (needed before paper_ingestion)"
  # --profile perf is required: the perf overlay assigns paper_ingestion to
  # profiles:["perf"], making it invisible without the flag; compose then
  # refuses to validate dashboard's depends_on: paper_ingestion.
  ( cd "${REPO_ROOT}" && \
    LETSENCRYPT_DOMAIN=local LETSENCRYPT_EMAIL=local@local.dev \
    docker compose --env-file .env --env-file versions.env \
      -f docker-compose.yml -f docker-compose.perf.yml --profile perf \
      up -d qdrant ollama litellm ) \
    || die "supporting services pre-start failed"
  # brief pause so qdrant is accepting connections before paper_ingestion starts
  sleep 5
  # Pull required Ollama models. ollama-bootstrap is excluded from
  # profile-stack-up (--no-deps) so on a fresh volume every embed/inference
  # call would get 404 from Ollama. Pull synchronously now so Stage 4 and
  # sweep Ollama legs don't race against a mid-flight model download.
  local embed_model ollama_ctr
  embed_model="${EMBEDDING_MODEL_NAME:-qwen3-embedding:4b}"
  ollama_ctr=$(docker ps --filter "name=ollama" --filter "status=running" \
    --format "{{.Names}}" 2>/dev/null | grep -v bootstrap | head -1)
  [[ -n "${ollama_ctr}" ]] || die "ollama container not found after pre-start"
  bench_log "  pulling embedding model: ${embed_model}"
  docker exec "${ollama_ctr}" ollama pull "${embed_model}" \
    || die "embedding model pull failed — Stage 4 embed would 404"
  bench_log "  pulling Pair A Ollama model: ${PAIR_A_OLLAMA_MODEL}"
  docker exec "${ollama_ctr}" ollama pull "${PAIR_A_OLLAMA_MODEL}" \
    || bench_warn "Pair A Ollama model pull failed — Ollama leg will fail"
  bench_log "  pulling Pair B Ollama model: ${PAIR_B_OLLAMA_MODEL}"
  docker exec "${ollama_ctr}" ollama pull "${PAIR_B_OLLAMA_MODEL}" \
    || bench_warn "Pair B Ollama model pull failed — Ollama leg will fail"
  # Ensure the perf probe dir exists and is user-owned before Docker binds it.
  # If Docker creates the dir (./shared/perf) it does so as root:root, making
  # it unwritable by the container's appuser → every embed_and_store call fails.
  mkdir -p "${REPO_ROOT}/shared/perf"
  # Force-remove paper_ingestion before make profile-stack-up so it is always
  # freshly created against the current shared/perf inode.  A running container
  # holds a reference to the inode it was started with; if shared/perf was
  # deleted and recreated (different inode) the container's /data/perf becomes
  # a dead mount and all probe writes fail with FileNotFoundError.
  docker rm -f jarvis-rd-assistant-paper_ingestion-1 2>/dev/null || true
  ( cd "${REPO_ROOT}" && make profile-stack-up ) || die "make profile-stack-up failed"
  # Wait for backend liveness. Use /health/live (no dependency chain) because
  # /health returns 503 while Ollama has no models loaded yet (fresh volume),
  # which would fail the -f flag for the full 300s window.
  local i
  for i in $(seq 1 60); do
    curl -sf -o /dev/null --max-time 5 "${BASE}/health/live" && break
    sleep 5
    [[ $i -eq 60 ]] && die "backend not live after 300s"
  done
  bench_log "backend live at ${BASE}"
}

# On a fresh-volume box the DB has no users.  POST /api/setup/admin is the
# first-run wizard endpoint: no-auth, wide-open when admin count = 0, returns
# 409 if an admin already exists (idempotent via the configured check below).
provision_admin_if_needed() {
  local status_json configured
  status_json="$(curl -s --max-time 10 "${BASE}/api/setup/status" 2>/dev/null || true)"
  configured="$(echo "${status_json}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("true" if d.get("configured") else "false")' \
    2>/dev/null || echo "unknown")"
  if [[ "${configured}" == "true" ]]; then
    bench_log "  admin already provisioned — skipping setup"
    return 0
  fi
  bench_log "  fresh DB — provisioning bench admin via /api/setup/admin"
  local sc
  sc="$(curl -s -o "${OUT_DIR}/.setup_admin.json" -w '%{http_code}' --max-time 15 \
          -X POST "${BASE}/api/setup/admin" \
          -H "Content-Type: application/json" \
          -d '{"email":"bench@local.bench"}' 2>/dev/null || echo 000)"
  [[ "${sc}" == "200" ]] || die "setup/admin → HTTP ${sc} — admin provisioning failed (see .setup_admin.json)"
  bench_log "  bench admin provisioned (bench@local.bench)"
}

# Mint the owner session early — if this fails, C1 is uncomputable; abort
# before wasting GPU hours.
mint_session() {
  local key code
  key="$(cat "${API_KEY_FILE}")"
  code="$(curl -s -o "${OUT_DIR}/.auth.json" -w '%{http_code}' --max-time 15 \
            -X POST "${BASE}/api/auth/api-key-session" \
            -H "X-API-Key: ${key}" -H "Content-Type: application/json" \
            -c "${COOKIE_JAR}" -d '{}' 2>/dev/null || echo 000)"
  [[ "${code}" == "200" ]] && grep -q jarvis_session "${COOKIE_JAR}" 2>/dev/null \
    || die "api-key-session → HTTP ${code} (need single-tenant + admin OR API_KEY_LOGIN_ENABLED). C1 uncomputable."
  bench_log "owner session minted"
}

# =============================================================================
# Stage 4 — Deterministic corpus seed (mandatory: box has no papers)
# =============================================================================
stage_seed() {
  bench_log "Stage 4: seed corpus — query='${BENCH_SEED_QUERY}' max=${BENCH_SEED_MAX}"
  # NOTE structured arXiv syntax — NL queries hit the ArxivSource
  # literal-phrase bug and return 0 results.
  local sc
  sc="$(curl -s -o "${OUT_DIR}/.seed_search.json" -w '%{http_code}' --max-time 60 \
          -X POST "${BASE}/api/search" -b "${COOKIE_JAR}" \
          -H "Content-Type: application/json" \
          -d "{\"query\":\"${BENCH_SEED_QUERY}\",\"source_types\":[\"arxiv\"],\"max_results\":${BENCH_SEED_MAX}}" \
          2>/dev/null || echo 000)"
  [[ "${sc}" == "200" ]] || die "POST /api/search → HTTP ${sc}"

  # /api/search returns list[PaperCreate] (no DB id); ids come from brief.
  local ids
  ids="$(curl -s --max-time 15 -b "${COOKIE_JAR}" "${BASE}/api/papers/brief" 2>/dev/null \
          | python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ".join(str(p["id"]) for p in (d if isinstance(d,list) else d.get("papers",[]))))' \
          2>/dev/null || true)"
  [[ -n "${ids}" ]] || die "no papers visible after seed search (arXiv down? query syntax?)"

  local id ok=0
  for id in ${ids}; do
    curl -s -o /dev/null --max-time 60 -X POST "${BASE}/api/download-pdf/${id}" \
      -b "${COOKIE_JAR}" -H "Content-Type: application/json" -d '{}' 2>/dev/null || true
    local pr
    pr="$(curl -s --max-time 600 -X POST "${BASE}/api/process-pdf/${id}?sync=true" \
            -b "${COOKIE_JAR}" -H "Content-Type: application/json" -d '{}' 2>/dev/null || true)"
    echo "${pr}" >> "${OUT_DIR}/seed-process.jsonl"
    echo "${pr}" | grep -q '"chunk_count"' && ok=$((ok+1))
  done
  [[ ${ok} -ge 1 ]] || die "no paper embedded (0 chunk_count across ${ids})"
  { echo "seed_paper_ids=${ids}"; echo "seed_embedded_ok=${ok}"; } >> "${OUT_DIR}/env.txt"
  cp "${OUT_DIR}/seed-process.jsonl" "${OUT_DIR}/seed-manifest.jsonl" 2>/dev/null || true
  bench_log "seed OK (${ok} papers embedded)"
}

# =============================================================================
# Sweep — one engine × one pair × one concurrency
# =============================================================================
do_sweep() {
  local pair="$1" engine="$2" conc="$3" model="$4"
  local sdir="${OUT_DIR}/${pair}_${engine}_c${conc}"
  mkdir -p "${sdir}"
  bench_log "  sweep ${pair}/${engine} c=${conc} model=${model}"
  probe_truncate

  # gpu probe in background for this sweep window
  local sentinel="${sdir}/.gpu.stop"
  ( OUT_DIR="${sdir}" PERF_GPU_PROBE_STOP="${sentinel}" PERF_CONCURRENCY="${conc}" \
    bash "${REPO_ROOT}/scripts/perf/gpu_probe.sh" ) & local gpid=$!

  # RAG_CONCURRENCY=conc so scenario_c_rag_ask reflects the target concurrency
  # (loadgen defaults RAG fan-out to <=3; C1 must measure at the real level).
  OUT_DIR="${sdir}" PERF_CONCURRENCY="${conc}" RAG_CONCURRENCY="${conc}" \
  PERF_PROBE_ENABLED=1 PERF_PROBE_PATH="${sdir}/perf-probe.jsonl" \
  PAPER_INGESTION_HOST_PORT="${PORT}" \
  bash "${REPO_ROOT}/scripts/perf/loadgen.sh" \
    || bench_warn "loadgen non-zero for ${pair}/${engine}/c${conc} (partial)"

  touch "${sentinel}"; wait "${gpid}" 2>/dev/null || true
  probe_collect "${sdir}/perf-probe.jsonl" || true

  read -r p50 p95 < <(rag_ask_p50_p95 "${sdir}/loadgen-summary.csv")
  echo "${pair},${engine},${conc},${p50},${p95}" >> "${OUT_DIR}/c1-raw.csv"
}

# Assert smart→engine actually routes (vLLM success counter delta).
assert_routing() {
  local engine="$1" before after
  before="$(vllm_success_total)"; before="${before:-}"
  curl -s -o /dev/null --max-time 120 -X POST "${BASE}/api/ask" \
    -b "${COOKIE_JAR}" -H "Content-Type: application/json" \
    -d '{"question":"Summarize one key method.","decompose":false}' 2>/dev/null || true
  after="$(vllm_success_total)"; after="${after:-}"
  if [[ "${engine}" == "vllm" ]]; then
    [[ -n "${after}" && -n "${before}" && "${after}" -gt "${before}" ]] \
      || die "vLLM routing UNPROVEN (success_total ${before}→${after}) for the vLLM leg"
  else
    if [[ -n "${after}" && -n "${before}" && "${after}" -gt "${before}" ]]; then
      die "vLLM received traffic during the OLLAMA leg (${before}→${after}) — alias not switched"
    fi
  fi
  bench_log "  routing asserted (${engine}: ${before:-NA}→${after:-NA})"
}

assert_ask_200() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 -X POST "${BASE}/api/ask" \
            -b "${COOKIE_JAR}" -H "Content-Type: application/json" \
            -d '{"question":"What is discussed?","decompose":true}' 2>/dev/null || echo 000)"
  [[ "${code}" == "200" ]] || die "/api/ask → HTTP ${code} (expected 200; RAG-DB-1 / stack issue)"
}

ollama_ensure() {
  local m="$1"
  curl -sf --max-time 5 "${OLLAMA_BASE}/api/tags" 2>/dev/null | grep -q "\"${m}\"" && return 0
  bench_log "  pulling ollama model ${m} (one-time)"
  curl -s --max-time 1800 -X POST "${OLLAMA_BASE}/api/pull" \
    -d "{\"name\":\"${m}\"}" >/dev/null 2>&1 \
    || die "ollama pull ${m} failed"
}

vllm_up() {
  local model="$1"
  bench_log "  (re)creating vLLM service: model=${model} util=${VLLM_GPU_MEMORY_UTILIZATION} image=${VLLM_IMAGE}"
  ( cd "${REPO_ROOT}" && VLLM_MODEL="${model}" ${COMPOSE} --profile vllm up -d --force-recreate vllm ) \
    || die "vLLM compose up failed for ${model}"
  local i
  for i in $(seq 1 60); do
    curl -sf -o /dev/null --max-time 3 "http://localhost:${VLLM_HOST_PORT}/health" && { bench_log "  vLLM healthy"; return 0; }
    sleep 10
  done
  die "vLLM not healthy after 600s for ${model} (VRAM? image arch? model id?)"
}

# =============================================================================
# Stage 8 — answer-quality capture (SSE, human sign-off artifact)
# =============================================================================
quality_capture() {
  local pair="$1" engine="$2"
  local qd="${OUT_DIR}/quality/${pair}/${engine}"
  mkdir -p "${qd}"
  local prompts=(
    '{"question":"Summarize the core contribution of one seeded paper.","decompose":false}'
    '{"question":"What methods and results recur across the seeded papers?","decompose":true}'
    '{"question":"What are the stated limitations or open problems?","decompose":true}'
    '{"question":"Explain the main evaluation setup in plain terms.","decompose":false}'
  )
  local n=0
  for body in "${prompts[@]}"; do
    n=$((n+1))
    sse_drain "${BASE}" "${COOKIE_JAR}" "${body}" "${qd}/q${n}.txt" || true
  done
  bench_log "  quality captured (${engine}) → ${qd}"
}

write_quality_diff() {
  local pair="$1"
  local vd="${OUT_DIR}/quality/${pair}/vllm" od="${OUT_DIR}/quality/${pair}/ollama"
  local md="${OUT_DIR}/quality/${pair}/DIFF.md"
  [[ -d "${vd}" && -d "${od}" ]] || return 0
  { echo "# Quality DIFF — Pair ${pair} (human sign-off required)"
    echo
    echo "Latency win is not a win if answers degrade. Eyeball each pair."
    for f in "${vd}"/q*.txt; do
      local b; b="$(basename "$f")"
      echo; echo "## ${b}"
      echo; echo "### vLLM"; echo '```'; cat "${vd}/${b}" 2>/dev/null; echo '```'
      echo; echo "### Ollama"; echo '```'; cat "${od}/${b}" 2>/dev/null; echo '```'
    done
  } > "${md}"
  bench_log "  DIFF → ${md}"
}

# =============================================================================
# Stage 7 — compute C1/C2/C3
# =============================================================================
compute_verdict() {
  bench_log "Stage 7: compute C1/C2/C3"
  python3 - "${OUT_DIR}" <<'PY' > "${OUT_DIR}/c1_c2_c3.json" 2>/dev/null || bench_warn "verdict computation degraded"
import sys, os, json, glob, statistics
out = sys.argv[1]
res = {"gate": "C1>=30% @conc>=4 AND C2<10% AND manual quality sign-off (no soak)",
       "pairs": {}}
raw = os.path.join(out, "c1-raw.csv")
data = {}
if os.path.exists(raw):
    for ln in open(raw):
        pair, eng, c, p50, p95 = (ln.strip().split(",") + ["", "", "", "", ""])[:5]
        try: p95 = float(p95)
        except: p95 = None
        data.setdefault(pair, {}).setdefault(c, {})[eng] = p95

def embed_p95(pair, eng):
    vals = []
    for f in glob.glob(os.path.join(out, f"{pair}_{eng}_c*", "perf-probe.jsonl")):
        for ln in open(f):
            try:
                o = json.loads(ln)
                if o.get("span") == "embed_texts_post" and "ms" in o:
                    vals.append(float(o["ms"]))
            except: pass
    if not vals: return None
    vals.sort()
    k = max(0, int(round(0.95 * len(vals) + 0.5)) - 1)
    return vals[min(k, len(vals)-1)]

for pair, ccs in data.items():
    pr = {"concurrency": {}}
    for c, eng in ccs.items():
        ov, vv = eng.get("ollama"), eng.get("vllm")
        imp = None
        if ov and vv and ov > 0:
            imp = round((ov - vv) / ov * 100.0, 1)
        pr["concurrency"][c] = {"ollama_p95_s": ov, "vllm_p95_s": vv,
                                "improvement_pct": imp,
                                "C1_pass": (imp is not None and imp >= 30.0 and int(c) >= 4)}
    e_o, e_v = embed_p95(pair, "ollama"), embed_p95(pair, "vllm")
    drift = round(abs(e_v - e_o) / e_o * 100.0, 1) if (e_o and e_v and e_o > 0) else None
    pr["C2_embed_p95_ms"] = {"ollama": e_o, "vllm": e_v, "drift_pct": drift,
                             "C2_pass": (drift is not None and drift < 10.0)}
    pr["C3"] = "structural: one profile-gated service, zero call-site, restored git-clean"
    pr["quality_signoff"] = "PENDING — review quality/%s/DIFF.md" % pair
    res["pairs"][pair] = pr

json.dump(res, sys.stdout, indent=2)
PY
}

write_results_md() {
  local rc="$1"
  { echo "# vLLM Confirmatory Bench — RESULTS (${TS})"
    echo
    echo "Exit rc: ${rc}  |  Aborted: ${ABORTED}"
    echo
    echo "## Environment"; echo '```'; cat "${OUT_DIR}/env.txt" 2>/dev/null; echo '```'
    echo
    echo "## C1/C2/C3"; echo '```json'; cat "${OUT_DIR}/c1_c2_c3.json" 2>/dev/null; echo '```'
    echo
    if [[ -f "${OUT_DIR}/ABORT.txt" ]]; then
      echo "## ABORT diagnostics"; echo '```'; cat "${OUT_DIR}/ABORT.txt"; echo '```'; echo
    fi
    echo "## Sign-off"
    echo "- Latency gate: see c1_c2_c3.json (C1_pass / C2_pass per pair)."
    echo "- Quality gate: open each quality/<pair>/DIFF.md and judge manually."
    echo "- No 48h soak (dropped by decision; re-trigger only on multi-user/public)."
  } > "${OUT_DIR}/RESULTS.md"
}

# =============================================================================
# Main
# =============================================================================
main() {
  stage_preflight
  stage_rebuild
  stage_boot
  provision_admin_if_needed
  mint_session
  stage_seed

  for pair in ${BENCH_PAIRS}; do
    local vm om
    if [[ "${pair}" == "A" ]]; then vm="${PAIR_A_VLLM_MODEL}"; om="${PAIR_A_OLLAMA_MODEL}"
    elif [[ "${pair}" == "B" ]]; then vm="${PAIR_B_VLLM_MODEL}"; om="${PAIR_B_OLLAMA_MODEL}"
    else bench_warn "unknown pair ${pair} — skipping"; continue; fi
    bench_log "=== Pair ${pair}: vLLM=${vm}  Ollama=${om} ==="

    # vLLM leg (container stays up across both legs → identical GPU residency)
    vllm_up "${vm}"
    run_or_die litellm_smart_to_vllm "${vm}"
    ( cd "${REPO_ROOT}" && ${COMPOSE} restart litellm ) || die "litellm restart failed"
    sleep 8
    assert_ask_200
    assert_routing vllm
    for c in ${BENCH_CONCURRENCY}; do do_sweep "${pair}" vllm "${c}" "${vm}"; done
    quality_capture "${pair}" vllm

    # Ollama leg (same model family; vLLM container left up)
    ollama_ensure "${om}"
    run_or_die litellm_smart_to_ollama "${om}"
    ( cd "${REPO_ROOT}" && ${COMPOSE} restart litellm ) || die "litellm restart failed"
    sleep 8
    assert_ask_200
    assert_routing ollama
    for c in ${BENCH_CONCURRENCY}; do do_sweep "${pair}" ollama "${c}" "${om}"; done
    quality_capture "${pair}" ollama
    write_quality_diff "${pair}"

    ( cd "${REPO_ROOT}" && ${COMPOSE} --profile vllm stop vllm ) || true
  done

  compute_verdict
  bench_log "DONE — bundle will be emitted by the finalize trap"
}

main "$@"
