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
# Plan / context: docs/perf/2026-05-18-vllm-confirmatory-bench-plan.md
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

# --- candidate file (optional; TSV on disk: tier<TAB>backend<TAB>model_id<TAB>vram_sim_mb<TAB>notes;
#     _iter_candidates converts to pipe-separated internally to preserve empty fields) ---
BENCH_CANDIDATES_FILE="${BENCH_CANDIDATES_FILE:-}"

_iter_candidates() {
  # Emits one line per candidate: tier|backend|model|vram_sim_mb|notes
  # (pipe-delimited internally to avoid bash IFS-whitespace tab-collapse on
  # empty fields; the on-disk TSV format stays tab-separated for human authoring.)
  if [ -n "${BENCH_CANDIDATES_FILE}" ]; then
    [ -f "${BENCH_CANDIDATES_FILE}" ] || die "BENCH_CANDIDATES_FILE not found: ${BENCH_CANDIDATES_FILE}" \
      "Try: scripts/perf/candidates.example.tsv"
    grep -vE '^[[:space:]]*(#|$)' "${BENCH_CANDIDATES_FILE}" \
      | awk -F'\t' 'NF>=3 {
          for (i=NF+1; i<=5; i++) $i=""
          print $1 "|" $2 "|" $3 "|" $4 "|" $5
        }'
  else
    # Back-compat: emit the existing PAIR_A/PAIR_B rows as candidates, filtered by BENCH_PAIRS
    for pair in ${BENCH_PAIRS}; do
      local p
      p="${pair,,}"   # normalise A→a, B→b
      case "${p}" in
        a|pair-a)
          printf 'pair-a|vllm|%s||\n'   "${PAIR_A_VLLM_MODEL}"
          printf 'pair-a|ollama|%s||\n' "${PAIR_A_OLLAMA_MODEL}"
          ;;
        b|pair-b)
          printf 'pair-b|vllm|%s||\n'   "${PAIR_B_VLLM_MODEL}"
          printf 'pair-b|ollama|%s||\n' "${PAIR_B_OLLAMA_MODEL}"
          ;;
      esac
    done
  fi
}

# v0.6.6 was pinned for sm_89/Ada reproducibility but is TWO ways too old:
# (a) Pair B Qwen3-8B-AWQ → bundled Transformers raises KeyError:'qwen3'
#     ("architecture not recognized"); (b) documented earlier: v0.6.6 ships
#     no sm_120 kernels → invalid on Blackwell. v0.11.0 is a recent stable
# that supports Qwen3+AWQ AND sm_89 (Ada, this box) AND sm_120 (Blackwell,
# future-proof). Still pinned (reproducible) and overridable via VLLM_IMAGE.
export VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.11.0}"
export VLLM_HOST_PORT="${VLLM_HOST_PORT:-8080}"
export VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.75}"
export VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"

# --- bench-only opt-in overrides (profile.sh never sets these) ----------------
# Exempts bench sweep requests from the default 10/minute /api/ask limit so
# c8 × 5 batches = 40 requests land cleanly instead of hitting 429s.
export ASK_RATE_LIMIT="${ASK_RATE_LIMIT:-1000/minute}"
# Allows Ollama to serve concurrent embed calls; default 2 serialises requests
# at c≥4, making C2 measure queue saturation rather than real throughput delta.
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-16}"

# --- BENCH_SMOKE: <8-min full-control-flow harness proof (verdict INVALID) --
# Runs every stage/trap/assert/restore path with trivial cost so the harness
# can be proven on the real box BEFORE committing to the 1-3h matrix.
BENCH_SMOKE="${BENCH_SMOKE:-0}"
if [[ "${BENCH_SMOKE}" == "1" ]]; then
  BENCH_PAIRS="A"
  BENCH_CONCURRENCY="1"
  BENCH_SEED_MAX="1"
  BENCH_RAG_BATCHES="${BENCH_RAG_BATCHES:-2}"   # keep smoke <~8min
  MIN_VRAM_MB="0"
  BENCH_REBUILD="0"
  PAIR_A_VLLM_MODEL="Qwen/Qwen2.5-0.5B-Instruct"
  PAIR_A_OLLAMA_MODEL="qwen2.5:0.5b-instruct"
  export VLLM_GPU_MEMORY_UTILIZATION="0.30"
  bench_warn "SMOKE MODE — harness proof only, the verdict is NOT valid"
  echo "BENCH_SMOKE=1 (harness proof only — verdict INVALID)" >> "${OUT_DIR}/env.txt"
fi

PORT="${PAPER_INGESTION_HOST_PORT:-8010}"
BASE="http://localhost:${PORT}"
OLLAMA_BASE="http://localhost:${OLLAMA_HOST_PORT:-11434}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vllm.yml"
API_KEY_FILE="${REPO_ROOT}/secrets/jarvis_api_key.txt"
COOKIE_JAR="${OUT_DIR}/.jarvis_session.cookies"

# --- compose project name (portability: NO hardcoded literals) ------------
# Honor -p/$COMPOSE_PROJECT_NAME/.env, else the Docker Compose v2 rule:
# basename(project dir) lowercased, [^a-z0-9_-] stripped, leading non-alnum
# stripped. Container/volume ops then prefer a LIVE compose label so a box
# whose dir differs from this one can never silently no-op a docker command.
_derive_project() {
  local p="${COMPOSE_PROJECT_NAME:-}"
  if [[ -z "$p" && -f "${REPO_ROOT}/.env" ]]; then
    p="$(grep -E '^COMPOSE_PROJECT_NAME=' "${REPO_ROOT}/.env" | tail -1 | cut -d= -f2- | tr -d ' "')"
  fi
  if [[ -z "$p" ]]; then
    p="$(basename "${REPO_ROOT}")"; p="${p,,}"; p="${p//[^a-z0-9_-]/-}"; p="${p#-}"
  fi
  echo "$p"
}
PROJECT="$(_derive_project)"

# _svc_container <compose-service> : the running/created container name for a
# service in THIS project, via compose labels (authoritative when up), else "".
_svc_container() {
  docker ps -a --filter "label=com.docker.compose.project=${PROJECT}" \
             --filter "label=com.docker.compose.service=$1" \
             --format '{{.Names}}' 2>/dev/null | head -1
}
pi_container()     { _svc_container paper_ingestion; }
ollama_container() { _svc_container ollama; }
qdrant_container() { _svc_container qdrant; }

ABORTED=0
_FINALIZED=0
# Populated by stage_preflight from nvidia-smi; used by per-candidate VRAM check.
vram_total_mb=0

# --- EXIT/INT/TERM trap: ALWAYS restore + bundle, exactly once ------------
finalize() {
  local rc=$?
  # Re-entrancy guard: INT/TERM set an exit code then fall through to EXIT,
  # so finalize would otherwise run twice (double restore + double tar).
  [[ "${_FINALIZED}" -eq 1 ]] && return 0
  _FINALIZED=1
  bench_log "finalize (rc=${rc}) — restoring + bundling"
  litellm_restore || bench_warn "litellm restore failed — inspect git status"
  # vLLM holds the GPU + port 8080; a dead daemon / hung compose must NOT
  # leave it resident to poison the next run. compose stop, else force-rm the
  # resolved container; both time-bounded.
  local vc
  vc="$(_svc_container vllm 2>/dev/null || true)"
  timeout 30 bash -c "cd '${REPO_ROOT}' && ${COMPOSE} --profile vllm stop vllm" >/dev/null 2>&1 \
    || { [[ -n "${vc}" ]] && timeout 20 docker rm -f "${vc}" >/dev/null 2>&1; } || true
  write_results_md "${rc}"
  if ! bench_redact_bundle_tree "${OUT_DIR}" || [[ ! -f "${OUT_DIR}/REDACTION-MANIFEST.txt" ]]; then
    bench_warn "BUNDLE_REDACTION_FAILED — not creating tarball. Raw local results kept at:"
    bench_warn "  ${OUT_DIR}  — inspect/redact manually before sharing."
  elif ( cd "$(dirname "${OUT_DIR}")" && tar czf "${OUT_DIR}.tar.gz" "$(basename "${OUT_DIR}")" ); then
    bench_log "BUNDLE → ${OUT_DIR}.tar.gz"
  else
    bench_warn "BUNDLE_FAILED — tar czf errored (disk full?). Raw results kept at:"
    bench_warn "  ${OUT_DIR}  — copy this DIRECTORY back manually (no valid tarball)."
  fi
  if [[ "${ABORTED}" -eq 1 || ${rc} -ne 0 ]]; then
    bench_warn "Run ended ABORTED/non-zero — bundle is partial+diagnostic."
  fi
}
trap finalize EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
    if [[ "${vram}" =~ ^[0-9]+$ ]]; then
      vram_total_mb="${vram}"
      echo "gpu_vram_total_mb=${vram}" >> "${OUT_DIR}/env.txt"
      # Soft floor: warn but do NOT hard-die; per-candidate VRAM check handles skips.
      (( vram < MIN_VRAM_MB )) && \
        bench_warn "VRAM ${vram}MB < soft floor ${MIN_VRAM_MB}MB — candidates requiring more will be skipped per-row"
    else
      # Defensive: a valid box can report an odd format. Don't hard-die on a
      # parse miss — record + warn + continue (only a CONFIRMED integer below
      # the floor is a hard abort).
      echo "gpu_vram_total_mb=UNPARSED(${vram})" >> "${OUT_DIR}/env.txt"
      bench_warn "nvidia-smi memory.total unparseable ('${vram}') — VRAM floor NOT enforced this run"
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
    echo "compose_project=${PROJECT}"
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
  local qd_ctr qd_vol
  qd_ctr="$(qdrant_container)"
  if [[ -n "${qd_ctr}" ]]; then
    qd_vol="$(docker inspect "${qd_ctr}" \
      --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Name}}{{end}}{{end}}' \
      2>/dev/null || true)"
  fi
  [[ -n "${qd_vol:-}" ]] || qd_vol="${PROJECT}_qdrant_data"
  # If the project IS up but we still resolved no volume, our derivation is
  # wrong — fail LOUD rather than silently skip the wipe (the old literal
  # fallback's exact failure mode on a differently-named box).
  if [[ -n "${qd_ctr}" && -z "${qd_vol}" ]]; then
    die "could not resolve qdrant volume for project '${PROJECT}' though qdrant is up — refusing to skip the dimension-pin wipe"
  fi
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
      up -d --force-recreate qdrant ollama litellm ) \
    || die "supporting services pre-start failed"
  # Wait for qdrant readiness via its compose healthcheck (qdrant has no host
  # port and no curl/python in-image, so docker-health is the only signal).
  local qc qi
  qc="$(qdrant_container)"
  for qi in $(seq 1 30); do
    [[ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${qc}" 2>/dev/null)" == "healthy" ]] && break
    sleep 2
    [[ $qi -eq 30 ]] && bench_warn "qdrant health not confirmed in 60s — proceeding (paper_ingestion will retry its connection)"
  done
  # Pull required Ollama models. ollama-bootstrap is excluded from
  # profile-stack-up (--no-deps) so on a fresh volume every embed/inference
  # call would get 404 from Ollama. Pull synchronously now so Stage 4 and
  # sweep Ollama legs don't race against a mid-flight model download.
  local embed_model ollama_ctr
  embed_model="${EMBEDDING_MODEL_NAME:-qwen3-embedding:4b}"
  ollama_ctr="$(ollama_container)"
  [[ -n "${ollama_ctr}" ]] || die "ollama container not found after pre-start (project '${PROJECT}')"
  bench_log "  pulling embedding model: ${embed_model}"
  docker exec "${ollama_ctr}" ollama pull "${embed_model}" \
    || die "embedding model pull failed — Stage 4 embed would 404"
  # Pull EVERY active ollama-backed alias model in litellm/config.yaml. The
  # /api/ask?decompose=true path calls the `fast` alias for query
  # decomposition (NOT just `smart`); if `fast` (ollama/qwen3:4b) is unpulled,
  # decompose 404s, RAG degrades to "No relevant information found", the
  # smart/vLLM call is never reached and routing is unprovable. Parsing the
  # config (not a hardcoded list) future-proofs every alias the pipeline uses
  # (fast, baseline smart, embed). Idempotent.
  local m
  while IFS= read -r m; do
    [[ -n "${m}" ]] || continue
    bench_log "  pulling litellm alias model: ${m}"
    docker exec "${ollama_ctr}" ollama pull "${m}" \
      || bench_warn "pull ${m} failed — any RAG path using it (e.g. decompose=fast) will degrade"
  done < <(awk '
      /^[[:space:]]*#/ {next}
      /model:[[:space:]]*"ollama\// {
        if (match($0, /"ollama\/[^"]+"/)) {
          s=substr($0,RSTART+8,RLENGTH-9); if (!(s in seen)) {seen[s]=1; print s}
        }
      }' "${REPO_ROOT}/litellm/config.yaml")
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
  local pi_prev
  pi_prev="$(pi_container)"
  if [[ -n "${pi_prev}" ]]; then
    docker rm -f "${pi_prev}" >/dev/null 2>&1 || true
    bench_log "  removed prior paper_ingestion container (${pi_prev})"
  else
    bench_log "  no prior paper_ingestion container (fresh project '${PROJECT}')"
  fi
  ( cd "${REPO_ROOT}" && make profile-stack-up ) || die "make profile-stack-up failed"
  # Wait for backend liveness. Use /health/live (no dependency chain) because
  # /health returns 503 while Ollama has no models loaded yet (fresh volume),
  # which would fail the -f flag for the full 300s window.
  local i boot_cap boot_iter
  boot_cap="${BENCH_BOOT_TIMEOUT_S:-300}"
  boot_iter=$(( boot_cap / 5 )); [[ ${boot_iter} -lt 1 ]] && boot_iter=1
  for i in $(seq 1 "${boot_iter}"); do
    curl -sf -o /dev/null --max-time 5 "${BASE}/health/live" && break
    sleep 5
    [[ $i -eq ${boot_iter} ]] && die "backend not live after ${boot_cap}s (raise BENCH_BOOT_TIMEOUT_S for slow boxes)"
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
_SESSION_MINTED=0
mint_session() {
  # Sole writer of COOKIE_JAR, exactly once per process. Resuming an old
  # OUT_DIR is unsupported (each run = fresh per-TS dir, see OUT_DIR).
  [[ "${_SESSION_MINTED}" -eq 1 ]] && { bench_log "session already minted — skipping"; return 0; }
  local key code
  key="$(cat "${API_KEY_FILE}")"
  code="$(curl -s -o "${OUT_DIR}/.auth.json" -w '%{http_code}' --max-time 15 \
            -X POST "${BASE}/api/auth/api-key-session" \
            -H "X-API-Key: ${key}" -H "Content-Type: application/json" \
            -c "${COOKIE_JAR}" -d '{}' 2>/dev/null || echo 000)"
  [[ "${code}" == "200" ]] && grep -q jarvis_session "${COOKIE_JAR}" 2>/dev/null \
    || die "api-key-session → HTTP ${code} (need single-tenant + admin OR API_KEY_LOGIN_ENABLED). C1 uncomputable."
  _SESSION_MINTED=1
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
  sc="$(curl -s -o "${OUT_DIR}/.seed_search.json" -w '%{http_code}' --max-time "${BENCH_SEED_MAX_TIME:-60}" \
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

  # force=true is MANDATORY here: this bench wipes the Qdrant volume every run
  # (dimension-pin guard) but postgres `paper_chunks` PERSISTS. Without force,
  # process-pdf sees existing_count>0 in postgres, returns "already_processed"
  # and re-embeds NOTHING into the fresh Qdrant → RAG retrieves 0 chunks →
  # "No relevant information found". force makes it delete stale refs and
  # genuinely re-embed. Safe on a fresh box too (existing_count=0 → no-op).
  # Success now requires status=="processed" (a real embed); accepting
  # "already_processed" was the false-positive that masked an empty Qdrant.
  local id ok=0
  for id in ${ids}; do
    curl -s -o /dev/null --max-time 60 -X POST "${BASE}/api/download-pdf/${id}" \
      -b "${COOKIE_JAR}" -H "Content-Type: application/json" -d '{}' 2>/dev/null || true
    local pr
    pr="$(curl -s --max-time 600 -X POST "${BASE}/api/process-pdf/${id}?sync=true&force=true" \
            -b "${COOKIE_JAR}" -H "Content-Type: application/json" -d '{}' 2>/dev/null || true)"
    echo "${pr}" >> "${OUT_DIR}/seed-process.jsonl"
    echo "${pr}" | grep -Eq '"status":[[:space:]]*"processed"' && ok=$((ok+1))
  done
  [[ ${ok} -ge 1 ]] || die "no paper genuinely re-embedded (0 status=processed across ${ids}) — Qdrant would be empty; see seed-process.jsonl"
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
  # LOADGEN_STRICT=1: a non-runnable Scenario C aborts the bench HERE with a
  # loadgen-FATAL.txt in the bundle, not a silent NA row found hours later.
  # All loadgen output is teed to loadgen.log so the bundle always carries
  # the proof of why a sweep failed (no more live back-and-forth).
  local lg_rc
  # RAG_BATCHES>1 → scenario_c_rag_ask p95 over n=conc×batches (default 5),
  # not a meaningless max-of-conc. BENCH_RAG_BATCHES overrides.
  OUT_DIR="${sdir}" LOADGEN_STRICT=1 PERF_CONCURRENCY="${conc}" RAG_CONCURRENCY="${conc}" \
  RAG_BATCHES="${BENCH_RAG_BATCHES:-5}" \
  PERF_PROBE_ENABLED=1 PERF_PROBE_PATH="${sdir}/perf-probe.jsonl" \
  PAPER_INGESTION_HOST_PORT="${PORT}" \
  bash "${REPO_ROOT}/scripts/perf/loadgen.sh" > "${sdir}/loadgen.log" 2>&1
  lg_rc=$?

  touch "${sentinel}"; wait "${gpid}" 2>/dev/null || true
  probe_collect "${sdir}/perf-probe.jsonl" || true

  if [[ ${lg_rc} -ne 0 ]]; then
    die "loadgen exited ${lg_rc} for ${pair}/${engine}/c${conc} — see ${sdir}/loadgen.log + ${sdir}/loadgen-FATAL.txt"
  fi
  read -r p50 p95 < <(rag_ask_p50_p95 "${sdir}/loadgen-summary.csv")
  if [[ -z "${p95}" || "${p95}" == "NA" ]]; then
    die "C1 unmeasured for ${pair}/${engine}/c${conc}: no scenario_c_rag_ask row (see ${sdir}/loadgen.log)"
  fi
  echo "${pair},${engine},${conc},${p50},${p95}" >> "${OUT_DIR}/c1-raw.csv"
}

# Self-diagnosing routing-failure capture → bundle (no more live round-trips).
_routing_diag() {
  local engine="$1" before="$2" after="$3" f="${OUT_DIR}/routing-fail-${engine}.txt"
  {
    echo "ROUTING FAIL — engine=${engine}  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "vllm_success_total: before='${before}' after='${after}'  (empty = metric absent = 0 successes)"
    echo
    echo "--- /api/ask routing probe response body (.routing_probe_${engine}.txt) ---"
    cat "${OUT_DIR}/.routing_probe_${engine}.txt" 2>/dev/null | head -c 4000
    echo; echo "--- vLLM /metrics request_success_total ---"
    curl -s --max-time 5 "http://localhost:${VLLM_HOST_PORT}/metrics" 2>/dev/null \
      | grep '^vllm:request_success_total' || echo "(absent — vLLM has completed 0 successful requests)"
    echo "--- litellm logs (tail 60; the actual upstream error) ---"
    docker logs "$(_svc_container litellm)" --tail 60 2>&1 \
      | grep -iE 'error|exception|not found|connect|fallback|model group|vllm|ollama|qwen' | tail -30
    echo "--- active litellm smart/fast aliases ---"
    grep -nE 'model_name:|model:|api_base:' "${REPO_ROOT}/litellm/config.yaml" \
      | grep -v '^[0-9]*:[[:space:]]*#' | grep -iE 'smart|fast' | head
  } > "${f}" 2>&1
  bench_warn "routing diagnostics → ${f} (in bundle)"
}

# Assert smart→engine actually routes (vLLM success counter delta). The
# counter line is ABSENT until vLLM completes its first success, so "empty"
# means 0 (NOT "scrape broken") — normalise empty→0 so a correct 0→1
# transition counts as PROVEN (the prior -n guard wrongly died on it).
assert_routing() {
  local engine="$1" before after b a ok=1
  before="$(vllm_success_total)"
  curl -s -o "${OUT_DIR}/.routing_probe_${engine}.txt" --max-time 120 -X POST "${BASE}/api/ask" \
    -b "${COOKIE_JAR}" -H "Content-Type: application/json" \
    -d '{"question":"Summarize one key method.","decompose":false}' 2>/dev/null || true
  after="$(vllm_success_total)"
  b="${before:-0}"; [[ "${b}" =~ ^[0-9]+$ ]] || b=0
  a="${after:-0}";  [[ "${a}" =~ ^[0-9]+$ ]] || a=0
  if [[ "${engine}" == "vllm" ]]; then
    (( a > b )) || ok=0
  else
    (( a > b )) && ok=0   # vLLM must NOT gain traffic during the Ollama leg
  fi
  if [[ ${ok} -eq 0 ]]; then
    _routing_diag "${engine}" "${before}" "${after}"
    if [[ "${engine}" == "vllm" ]]; then
      die "vLLM routing UNPROVEN (success_total ${before:-absent}→${after:-absent}) — see routing-fail-vllm.txt in bundle"
    else
      die "vLLM got traffic during OLLAMA leg (${before:-absent}→${after:-absent}) — alias not switched; see routing-fail-ollama.txt"
    fi
  fi
  bench_log "  routing asserted (${engine}: ${before:-absent}→${after:-absent})"
}

assert_ask_200() {
  # litellm was just `compose restart`ed; compose returns while the container
  # is still "Restarting" and litellm's cold start (config reload + provider
  # wiring) is variable. BOTH the chat (smart) AND the query-embed path route
  # through litellm, so a premature /api/ask raises httpx.ConnectError deep in
  # embed_texts → RuntimeError("Embedding service unavailable") → generic HTTP
  # 500 (NOT 502/503). Evidence: REDACTED-HOST 20:49 abort. Therefore:
  #   (1) gate on litellm's OWN readiness first — after that a 500 is real;
  #   (2) then poll /api/ask; 500 retryable only within a bounded post-ready
  #       window, then a final die that still surfaces the code (RAG-DB-1
  #       diagnostic intent preserved).
  local i code deadline
  for i in $(seq 1 40); do
    docker exec "$(pi_container)" python3 -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://litellm:4000/health/readiness',timeout=5).status==200 else 1)" \
      >/dev/null 2>&1 && { bench_log "  litellm ready (after $(( (i-1)*3 ))s)"; break; }
    [[ $i -eq 40 ]] && die "litellm not ready 120s after restart — cannot assert /api/ask"
    sleep 3
  done
  deadline=$(( SECONDS + 180 ))
  local bodyf="${OUT_DIR}/.ask_probe.json"
  while :; do
    code="$(curl -s -o "${bodyf}" -w '%{http_code}' --max-time 120 -X POST "${BASE}/api/ask" \
              -b "${COOKIE_JAR}" -H "Content-Type: application/json" \
              -d '{"question":"What is discussed?","decompose":true}' 2>/dev/null || echo 000)"
    if [[ "${code}" == "200" ]]; then
      # HTTP 200 on this endpoint does NOT prove the LLM path worked: a failed
      # decompose (e.g. the `fast` alias model unpulled) still returns 200 with
      # a degraded "No relevant information found" body and never reaches the
      # smart/vLLM call. Treat that as not-ready (retry in-window) so the bench
      # never green-lights a broken pipeline into the sweeps.
      if grep -q 'No relevant information found' "${bodyf}" 2>/dev/null; then
        if (( SECONDS >= deadline )); then
          cp "${bodyf}" "${OUT_DIR}/ask-degraded.json" 2>/dev/null || true
          docker logs "$(_svc_container litellm)" --tail 60 2>&1 \
            | grep -iE 'error|not found|fallback|model group|connect|qwen' | tail -20 \
            > "${OUT_DIR}/ask-degraded-litellm.txt" 2>&1 || true
          die "/api/ask 200 but DEGRADED ('No relevant information found') +180s — LLM path broken (see ask-degraded*.{json,txt}); likely an unpulled litellm alias model"
        fi
        bench_log "  /api/ask degraded (LLM path not ready) — retrying"; sleep 6; continue
      fi
      return 0
    fi
    case "${code}" in
      000|500|502|503|504)
        (( SECONDS >= deadline )) && die "/api/ask → HTTP ${code} after litellm-ready +180s (RAG-DB-1 / stack issue)"
        bench_log "  /api/ask warming (HTTP ${code}) — retrying"; sleep 6 ;;
      *) die "/api/ask → HTTP ${code} (expected 200; RAG-DB-1 / stack issue)" ;;
    esac
  done
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
  # NOTE: failures here return 1 (NOT die) so the caller can record+skip the
  # pair and still verdict the pairs that DID complete (the original plan's
  # "Pair A is never blocked by Pair B" guarantee).
  ( cd "${REPO_ROOT}" && VLLM_MODEL="${model}" ${COMPOSE} --profile vllm up -d --force-recreate vllm ) \
    || { bench_warn "vLLM compose up failed for ${model}"; return 1; }
  # Poll /v1/models (NOT /health: the compose healthcheck can false-pass
  # before the model is actually loadable) and assert the requested model id
  # is the one being served — proves the engine is ready for THIS model.
  local i cap iter
  cap="${VLLM_BOOT_TIMEOUT_S:-900}"          # first-time HF weight pull on slow links > 600s
  iter=$(( cap / 10 )); [[ ${iter} -lt 1 ]] && iter=1
  for i in $(seq 1 "${iter}"); do
    if curl -sf --max-time 5 "http://localhost:${VLLM_HOST_PORT}/v1/models" 2>/dev/null \
         | grep -qF "\"${model}\""; then
      bench_log "  vLLM serving ${model} (after $(( (i-1)*10 ))s)"; return 0
    fi
    sleep 10
  done
  docker logs --tail 80 "$(_svc_container vllm)" > "${OUT_DIR}/vllm-boot-fail.log" 2>&1 || true
  bench_warn "vLLM not serving ${model} after ${cap}s (see vllm-boot-fail.log; VRAM? image arch? model id? raise VLLM_BOOT_TIMEOUT_S)"
  return 1
}

# =============================================================================
# Stage 8 — answer-quality capture (SSE, human sign-off artifact)
# =============================================================================
quality_capture() {
  local pair="$1" engine="$2"
  local qd="${OUT_DIR}/quality/${pair}/${engine}"
  mkdir -p "${qd}"
  local prompts_file="${REPO_ROOT}/scripts/perf/quality/prompts.jsonl"
  [ -f "${prompts_file}" ] || { bench_warn "prompts.jsonl missing: ${prompts_file}"; return 0; }

  local n=0
  while IFS= read -r line; do
    [ -z "${line}" ] && continue
    n=$((n+1))
    local id body
    id=$(printf '%s' "${line}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    body=$(printf '%s' "${line}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"question":d["question"],"decompose":d.get("decompose",False)}))')
    sse_drain "${BASE}" "${COOKIE_JAR}" "${body}" "${qd}/q$(printf '%02d' "${id}").txt" || true
  done < "${prompts_file}"

  bench_log "  quality captured (${engine}) → ${qd} (${n} prompts)"
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

emit_tier_rankings_skeleton() {
  bench_log "emit tier-rankings.json skeleton (judge populates)"
  python3 - "${OUT_DIR}" <<'PY' > "${OUT_DIR}/tier-rankings.json"
import json, os, sys, glob
out = sys.argv[1]
tiers = {}
for tc in glob.glob(os.path.join(out, "*/tier-context.json")):
    with open(tc) as f:
        ctx = json.load(f)
    tier = ctx["tier_label"]
    entry = {
        "model": ctx["model"],
        "backend": ctx["backend"],
        "sim_or_native": "sim" if ctx["sim_tier_flag"] else "native",
        "judge_score": None,
        "throughput_p95_at_c4": None,
        "throughput_p95_at_c8": None,
        "fits_at_sim_vram": ctx["sim_tier_flag"] or True,
        "tier_context_path": os.path.relpath(tc, out),
    }
    tiers.setdefault(tier, []).append(entry)
print(json.dumps({"tiers": tiers, "judge_run_at": None}, indent=2))
PY
}

write_results_md() {
  local rc="$1"
  { echo "# vLLM Confirmatory Bench — RESULTS (${TS})"
    echo
    if [[ "${BENCH_SMOKE:-0}" == "1" ]]; then
      echo "> ⚠ **SMOKE MODE** — harness proof only. The verdict below is NOT"
      echo "> valid (0.5B model, concurrency 1, 1-paper seed). Use only to"
      echo "> confirm the harness runs end-to-end before the real matrix."
      echo
    fi
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

# Record the GPU memory budget so a silent OOM-kill becomes a recorded,
# actionable caveat. The Ollama embedder is pinned (keep_alive:-1) and
# coexists with vLLM (util*total). If vLLM's reservation + the resident
# embedder exceeds ~97% of total, warn — do NOT auto-lower util (the
# operator decides; surfacing it is what matters for the verdict reader).
record_vram_budget() {
  local total emb_mb util reserve oc
  total="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
  [[ "${total}" =~ ^[0-9]+$ ]] || { echo "vram_budget=unknown (total unparsed)" >> "${OUT_DIR}/env.txt"; return 0; }
  oc="$(ollama_container)"
  # ollama ps SIZE column e.g. "10 GB" / "512 MB"; → MB integer.
  emb_mb="$(docker exec "${oc}" ollama ps 2>/dev/null | awk '
    NR>1 && $1!="" {
      sz=$3; un=$4
      if (un ~ /GB/) printf "%d", sz*1024
      else if (un ~ /MB/) printf "%d", sz
      else printf "0"
      exit
    }')"
  [[ "${emb_mb}" =~ ^[0-9]+$ ]] || emb_mb=0
  util="${VLLM_GPU_MEMORY_UTILIZATION:-0.90}"
  reserve="$(awk -v t="${total}" -v u="${util}" 'BEGIN{printf "%d", t*u}')"
  {
    echo "vram_budget: total_mb=${total} vllm_util=${util} vllm_reserve_mb=${reserve} embedder_resident_mb=${emb_mb}"
  } >> "${OUT_DIR}/env.txt"
  if awk -v r="${reserve}" -v e="${emb_mb}" -v t="${total}" \
       'BEGIN{exit !((r+e) > t*0.97)}'; then
    bench_warn "VRAM OOM RISK: vLLM reserve ${reserve}MB + pinned embedder ${emb_mb}MB > 97% of ${total}MB. Consider re-running with a lower VLLM_GPU_MEMORY_UTILIZATION."
    echo "vram_budget_warning=OOM_RISK (reserve+embedder > 0.97*total)" >> "${OUT_DIR}/env.txt"
  fi
}

# Record a pair as skipped (NOT a run failure) and preserve its boot log so
# the bundle explains why. Does NOT set ABORTED — a skipped pair with another
# pair completed is still a SUCCESSFUL bench (original plan: "Pair A is never
# blocked by Pair B"). compute_verdict still runs on completed pairs.
skip_pair() {
  local pair="$1" reason="$2"
  {
    echo "PAIR ${pair} SKIPPED $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "${reason}"
  } > "${OUT_DIR}/pair-${pair}-SKIPPED.txt"
  [[ -f "${OUT_DIR}/vllm-boot-fail.log" ]] && \
    cp "${OUT_DIR}/vllm-boot-fail.log" "${OUT_DIR}/pair-${pair}-vllm-boot-fail.log" 2>/dev/null || true
  echo "pair_${pair}=SKIPPED (${reason})" >> "${OUT_DIR}/env.txt"
  bench_warn "Pair ${pair} SKIPPED — ${reason}. Other pairs still verdicted."
}

# Warm the /api/ask path so the FIRST measured sweep isn't cold-start
# contaminated (observed: vLLM c4 ~88s uniform while c8 ~23s — the first
# sweep after (re)create ran cold). Fired for BOTH legs so neither engine
# is advantaged by warm-vs-cold. Best-effort; failures don't abort.
warmup_path() {
  local engine="$1" n
  for n in 1 2; do
    curl -s -o /dev/null --max-time 120 -X POST "${BASE}/api/ask" \
      -b "${COOKIE_JAR}" -H "Content-Type: application/json" \
      -d '{"question":"Briefly, what is attention?","decompose":true}' 2>/dev/null || true
  done
  bench_log "  ${engine} path warmed (2 throwaway /api/ask) before measured sweeps"
}

# =============================================================================
# Main
# =============================================================================
main() {
  # Re-run safety: truncate the C1 aggregate so a fresh OUT_DIR can never
  # inherit rows (defensive — OUT_DIR is per-TS, but cheap insurance).
  : > "${OUT_DIR}/c1-raw.csv"
  # Disk-hygiene heads-up (never auto-delete prior bundles — destructive).
  local nprev
  nprev="$(find "${REPO_ROOT}/artifacts/perf" -maxdepth 1 -name 'vllm-confirmatory-*' 2>/dev/null | wc -l | tr -d ' ')"
  [[ "${nprev}" -gt 10 ]] && bench_warn "artifacts/perf has ${nprev} prior bench dirs/tarballs — consider pruning to free disk"
  stage_preflight
  stage_rebuild
  stage_boot
  provision_admin_if_needed
  mint_session
  stage_seed
  record_vram_budget

  while IFS='|' read -r CAND_TIER CAND_BACKEND CAND_MODEL CAND_SIM_VRAM_MB CAND_NOTES; do
    bench_log "candidate: tier=${CAND_TIER} backend=${CAND_BACKEND} model=${CAND_MODEL} sim_vram=${CAND_SIM_VRAM_MB:-native}"
    # Sanitize model id for filesystem-safe pair names (avoid collisions
    # when multiple candidates share the same tier+backend).
    _safe_model="${CAND_MODEL//\//_}"
    _safe_model="${_safe_model//:/_}"
    pair="${CAND_TIER}_${CAND_BACKEND}_${_safe_model}"
    cand_dir="${OUT_DIR}/${pair}"

    # Per-candidate VRAM check
    if [ "${CAND_BACKEND}" = "vllm" ]; then
      required_mb="${CAND_SIM_VRAM_MB:-${vram_total_mb}}"
      if [ "${required_mb}" -gt "${vram_total_mb}" ]; then
        bench_warn "skip ${CAND_MODEL}: requires ${required_mb} MB, box has ${vram_total_mb} MB"
        continue
      fi
    fi

    # Compute GPU memory utilization fraction for vLLM candidates.
    util=""
    if [ "${CAND_BACKEND}" = "vllm" ]; then
      if [ -n "${CAND_SIM_VRAM_MB}" ]; then
        util=$(awk -v sim="${CAND_SIM_VRAM_MB}" -v total="${vram_total_mb}" 'BEGIN{printf "%.2f", sim/total}')
      else
        util="${VLLM_GPU_MEMORY_UTILIZATION:-0.75}"
      fi
      export VLLM_GPU_MEMORY_UTILIZATION="${util}"
    fi

    # Emit per-candidate tier-context.json for downstream judge labeling.
    mkdir -p "${cand_dir}"
    util_json="null"
    [ -n "${util}" ] && util_json="\"${util}\""
    cat > "${cand_dir}/tier-context.json" <<JSON
{
  "tier_label": "${CAND_TIER}",
  "backend": "${CAND_BACKEND}",
  "model": "${CAND_MODEL}",
  "native_vram_mb": ${vram_total_mb},
  "sim_vram_mb": ${CAND_SIM_VRAM_MB:-null},
  "sim_tier_flag": $([ -n "${CAND_SIM_VRAM_MB}" ] && echo true || echo false),
  "gpu_memory_utilization": ${util_json}
}
JSON

    if [[ "${CAND_BACKEND}" == "vllm" ]]; then
      # vLLM leg (container stays up for the sweep → consistent GPU residency).
      # vllm_up failure → record + skip this candidate (no sweep possible without
      # the vLLM container); compute_verdict still verdicts completed candidates.
      if ! vllm_up "${CAND_MODEL}"; then
        skip_pair "${pair}" "vLLM could not serve ${CAND_MODEL} (see pair-${pair}-vllm-boot-fail.log)"
        continue
      fi
      run_or_die litellm_smart_to_vllm "${CAND_MODEL}"
      ( cd "${REPO_ROOT}" && ${COMPOSE} restart litellm ) || die "litellm restart failed"
      assert_ask_200          # polls until litellm settles (no fixed sleep)
      assert_routing vllm
      warmup_path vllm        # kill first-sweep cold-start contamination
      for c in ${BENCH_CONCURRENCY}; do do_sweep "${pair}" vllm "${c}" "${CAND_MODEL}"; done
      quality_capture "${pair}" vllm
      write_quality_diff "${pair}"
      ( cd "${REPO_ROOT}" && ${COMPOSE} --profile vllm stop vllm ) || true
    elif [[ "${CAND_BACKEND}" == "ollama" ]]; then
      # Ollama leg
      ollama_ensure "${CAND_MODEL}"
      run_or_die litellm_smart_to_ollama "${CAND_MODEL}"
      ( cd "${REPO_ROOT}" && ${COMPOSE} restart litellm ) || die "litellm restart failed"
      assert_ask_200          # polls until litellm settles (no fixed sleep)
      assert_routing ollama
      warmup_path ollama      # symmetric warm-up (fairness: neither engine cold)
      for c in ${BENCH_CONCURRENCY}; do do_sweep "${pair}" ollama "${c}" "${CAND_MODEL}"; done
      quality_capture "${pair}" ollama
      write_quality_diff "${pair}"
    else
      bench_warn "unknown backend '${CAND_BACKEND}' for candidate ${CAND_TIER}/${CAND_MODEL} — skipping"
      continue
    fi
  done < <(_iter_candidates)

  compute_verdict
  emit_tier_rankings_skeleton
  bench_log "DONE — bundle will be emitted by the finalize trap"
}

main "$@"
