#!/usr/bin/env bash
# scripts/validate-hardware.sh — hardware validation helper for contributors.
#
# Brings up just enough of the stack to exercise the LLM backend (Ollama +
# LiteLLM + paper_ingestion), runs one tiny inference through the LiteLLM
# `smart` alias, and prints a copy-pasteable hardware report you can paste
# into a GitHub issue when certifying a new GPU/vendor combination.
#
# Runs fine with no GPU (reports the CPU tier honestly) and never hard-fails
# just because a GPU is absent.
#
# Usage:
#   scripts/validate-hardware.sh [--gpu cuda|rocm|vulkan|cpu]
#
#   --gpu   Force a specific compose overlay instead of auto-detecting one
#           from the host's GPU vendor (same vocabulary as setup.sh --gpu).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=setup_lib.sh
source "${SCRIPT_DIR}/setup_lib.sh"

# ---------------------------------------------------------------------------
# Output helpers — match setup.sh style.
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

info() { printf '%s[INFO]%s  %s\n'  "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n'  "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; }

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
GPU_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu)
      case "${2:-}" in
        cuda|rocm|vulkan|cpu) GPU_OVERRIDE="$2"; shift 2 ;;
        *) err "Invalid --gpu '${2:-}'. Expected: cuda|rocm|vulkan|cpu"; exit 1 ;;
      esac
      ;;
    --gpu=*)
      _v="${1#*=}"
      case "$_v" in
        cuda|rocm|vulkan|cpu) GPU_OVERRIDE="$_v"; shift ;;
        *) err "Invalid --gpu '$_v'. Expected: cuda|rocm|vulkan|cpu"; exit 1 ;;
      esac
      ;;
    -h|--help)
      awk '/^# Usage:/{flag=1} flag{ if ($0 !~ /^#/) exit; print substr($0,3) }' "$0"
      exit 0
      ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# GPU vendor + overlay detection (reuses setup_lib.sh — the same probes
# setup.sh itself uses, so "engaged overlay" here matches a real install).
# ---------------------------------------------------------------------------
GPU_VENDOR="$(detect_gpu_vendor)"

if [ -n "$GPU_OVERRIDE" ]; then
  GPU_CHOICE="$GPU_OVERRIDE"
  info "GPU overlay forced by --gpu: ${GPU_CHOICE}"
else
  case "$GPU_VENDOR" in
    nvidia)
      if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
        GPU_CHOICE=cuda
      else
        warn "NVIDIA GPU detected but the Docker nvidia runtime is not configured — validating the CPU path instead."
        GPU_CHOICE=cpu
      fi
      ;;
    amd)
      # ROCm auto-engages only with /dev/kfd; without it Vulkan is opt-in, so the
      # default-detected path validates CPU (mirrors setup.sh's GPU ladder).
      if [ -e /dev/kfd ]; then GPU_CHOICE=rocm; else GPU_CHOICE=cpu; fi
      ;;
    intel) GPU_CHOICE=cpu ;;   # Intel Vulkan is opt-in (--gpu vulkan)
    *)     GPU_CHOICE=cpu ;;
  esac
  info "Detected GPU vendor: ${GPU_VENDOR} -> overlay: ${GPU_CHOICE}"
fi

case "$GPU_CHOICE" in
  cuda)   OVERLAY_NAME="gpu" ;;
  rocm)   OVERLAY_NAME="rocm" ;;
  vulkan) OVERLAY_NAME="vulkan" ;;
  *)      OVERLAY_NAME="" ;;
esac
COMPOSE_FILE_STR="$(compute_compose_file "$OVERLAY_NAME" 0)"

VRAM_MB="$(resolve_gpu_vram_mb "$GPU_VENDOR" 2>/dev/null || true)"
VRAM_REPORT="n/a"
[ -n "$VRAM_MB" ] && VRAM_REPORT="${VRAM_MB} MB"

# ---------------------------------------------------------------------------
# Bring up just the LLM path: ollama -> (streamed) model pull -> paper_ingestion.
# paper_ingestion's dependency chain (docker-compose.yml) pulls in postgres,
# qdrant and litellm on its own -- dashboard/learning_engine add nothing to a
# hardware check, so we do not boot them here.
# ---------------------------------------------------------------------------
COMPOSE_ARGS=(--env-file .env)
if [ -f versions.env ]; then
  COMPOSE_ARGS+=(--env-file versions.env)
fi

dc() { COMPOSE_FILE="$COMPOSE_FILE_STR" docker compose "${COMPOSE_ARGS[@]}" "$@"; }

# wait_healthy <svc> [budget_seconds] — duplicated from setup.sh (setup.sh is
# not sourceable; only scripts/setup_lib.sh's helpers are shared).
wait_healthy() {
  local svc="$1"
  local budget="${2:-60}"
  local interval=3
  local elapsed=0
  local cid status

  while [ "$elapsed" -lt "$budget" ]; do
    cid="$(dc ps -q "$svc" 2>/dev/null | head -n 1 || true)"
    if [ -z "$cid" ]; then
      sleep "$interval"
      elapsed=$((elapsed + interval))
      continue
    fi
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    case "$status" in
      "")        info "$svc: no healthcheck defined — skipping wait."; return 0 ;;
      healthy)   ok "$svc: healthy"; return 0 ;;
      starting)  ;;
      unhealthy) err "$svc: unhealthy"; return 1 ;;
      *)         ;;
    esac
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  err "$svc: did not become healthy within ${budget}s."
  return 1
}

info "Starting Ollama (docker compose up -d ollama)"
dc up -d ollama
wait_healthy ollama 180 \
  || warn "Ollama is still starting — model inventory unknown; proceeding to the model pull."

info "Pulling models via ollama-bootstrap (streamed; a no-op if already pulled)..."
dc run --rm ollama-bootstrap

info "Starting paper_ingestion (brings up postgres, qdrant, and litellm as dependencies)"
dc up -d paper_ingestion
if ! wait_healthy paper_ingestion 600; then
  err "paper_ingestion did not become healthy — dumping the last 50 log lines."
  dc logs --tail 50 paper_ingestion >&2 || true
  exit 1
fi

# ---------------------------------------------------------------------------
# Probe the `smart` alias. It is NOT in litellm/config.yaml -- paper_ingestion
# registers it dynamically via a detached ~30s-cadence reconciler
# (litellm_reconciler.py), so a healthy paper_ingestion does not guarantee
# `smart` already exists. Poll with a fixed retry budget instead of a
# single-shot call; a 404/"model not found" is a not-ready signal, not a
# hard failure.
# ---------------------------------------------------------------------------

# resolve_secret NAME BASENAME — env var -> Docker Compose /run/secrets/<name>
# -> local secrets/<basename>.txt. Copy-adapted from
# scripts/production-readiness-check.sh (not sourceable).
resolve_secret() {
  local name="$1" basename="$2" val
  val="${!name:-}"
  if [ -z "$val" ] && [ -f "/run/secrets/${basename}" ]; then
    val="$(cat "/run/secrets/${basename}")"
  fi
  if [ -z "$val" ] && [ -f "${SCRIPT_DIR}/../secrets/${basename}.txt" ]; then
    val="$(cat "${SCRIPT_DIR}/../secrets/${basename}.txt")"
  fi
  printf '%s' "$val"
}

# Load .env so a bare LITELLM_MASTER_KEY/LITELLM_HOST_PORT in the shell
# environment still wins (resolve_secret's first tier), matching
# production-readiness-check.sh's own .env-loading convention.
if [ -f .env ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in
      \#*|"") continue ;;
    esac
    if [[ "$_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      _key="${BASH_REMATCH[1]}"; _val="${BASH_REMATCH[2]}"
      if [ -z "${!_key+x}" ]; then export "${_key}=${_val}"; fi
    fi
  done < .env
fi

LITELLM_MASTER_KEY="$(resolve_secret LITELLM_MASTER_KEY litellm_master_key)"
if [ -z "$LITELLM_MASTER_KEY" ]; then
  err "Could not resolve LITELLM_MASTER_KEY (checked env, /run/secrets, secrets/litellm_master_key.txt)."
  exit 1
fi
LITELLM_URL="http://127.0.0.1:${LITELLM_HOST_PORT:-4000}/v1/chat/completions"
PROBE_BODY='{"model":"smart","messages":[{"role":"user","content":"Reply with the single word OK."}],"max_tokens":8,"temperature":0}'

extract_content() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data["choices"][0]["message"]["content"].strip())
except Exception:
    print("")
'
}

RETRY_BUDGET=90
RETRY_INTERVAL=5
_elapsed=0
_success=0
_last_code="none"
_last_body=""
_latency=""

info "Waiting for the 'smart' alias to register (paper_ingestion reconciles it dynamically, up to ~${RETRY_BUDGET}s)..."
while [ "$_elapsed" -lt "$RETRY_BUDGET" ]; do
  _tmp="$(mktemp)"
  # Pass the master key via curl --config on stdin, never on argv (argv is
  # visible to other local users through ps / /proc/<pid>/cmdline).
  _meta="$(printf 'header = "Authorization: Bearer %s"\n' "$LITELLM_MASTER_KEY" \
    | curl -sS --max-time 30 -o "$_tmp" -w '%{http_code} %{time_total}' \
    --config - \
    -H 'Content-Type: application/json' \
    -d "$PROBE_BODY" \
    "$LITELLM_URL" 2>/dev/null || true)"
  _last_code="${_meta%% *}"
  _latency="${_meta##* }"
  _last_body="$(cat "$_tmp")"
  rm -f "$_tmp"
  if [ "$_last_code" = "200" ]; then
    _success=1
    break
  fi
  sleep "$RETRY_INTERVAL"
  _elapsed=$((_elapsed + RETRY_INTERVAL))
done

if [ "$_success" -ne 1 ]; then
  err "The 'smart' alias never became reachable via LiteLLM after ${RETRY_BUDGET}s (last HTTP status: ${_last_code})."
  printf '%s\n' "$_last_body" >&2
  exit 1
fi

REPLY="$(printf '%s' "$_last_body" | extract_content)"

# ---------------------------------------------------------------------------
# Hardware report — copy this block into the hardware-support issue form.
# ---------------------------------------------------------------------------
printf '\n'
printf '%s================ Hardware report (copy into the issue) ================%s\n' "$C_BOLD" "$C_RESET"
printf 'OS:                %s (%s)\n' "$(uname -s)" "$(uname -m)"
printf 'GPU vendor:        %s\n' "$GPU_VENDOR"
printf 'VRAM:              %s\n' "$VRAM_REPORT"
printf 'Compose overlay:   %s\n' "${OVERLAY_NAME:-none (CPU)}"
printf 'Model alias:       smart\n'
printf 'Inference latency: %ss\n' "$_latency"
printf 'Reply:             %s\n' "${REPLY:-<empty>}"
printf '%s==========================================================================%s\n' "$C_BOLD" "$C_RESET"
ok "Hardware validation passed."
