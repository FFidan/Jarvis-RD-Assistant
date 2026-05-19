#!/usr/bin/env bash
# scripts/perf/gpu_probe.sh — Track-B Phase-1: GPU/VRAM + run-metadata capture.
#
# PURPOSE
#   During a perf load window, polls nvidia-smi + Ollama /api/tags and appends
#   a JSON-lines timeseries to ${OUT_DIR}/gpu-timeseries.jsonl.
#   Emits ONCE ${OUT_DIR}/run-metadata.json with GPU model, VRAM totals,
#   concurrency config, LiteLLM alias→model map, git commit, and UTC timestamp.
#
# WHY CAPTURE, NEVER ASSUME
#   A run on a 16 GB GPU produces conservative lower-bound numbers for a 48 GB
#   box. Capturing actual hardware values (GPU model, total VRAM) into the
#   metadata file makes every artifact self-describing and comparable across
#   machines without guesswork.
#
# USAGE
#   # Start in background, run load driver, stop:
#   PERF_GPU_PROBE_STOP=/tmp/gpu_probe.stop OUT_DIR=/tmp/perf ./scripts/perf/gpu_probe.sh &
#   PROBE_PID=$!
#   # ... run load driver ...
#   touch /tmp/gpu_probe.stop       # sentinel-file stop
#   wait "${PROBE_PID}"
#
#   # Or stop via signal:
#   kill -TERM "${PROBE_PID}"
#
# ENV VARS
#   OUT_DIR                  output directory (default: REPO_ROOT/artifacts/perf/<UTC>)
#   PERF_GPU_POLL_SECONDS    polling interval in seconds (default: 2)
#   PERF_GPU_PROBE_STOP      path to sentinel file; touch it to stop polling (optional)
#   PERF_CONCURRENCY         concurrency level used by the load driver (optional, may be unset)
#   OLLAMA_HOST_PORT         Ollama port on host (default: 11434)
#   OLLAMA_MAX_LOADED_MODELS compose default: 3 (override via env)
#   OLLAMA_NUM_PARALLEL      compose default: 2 (override via env)
#
# DEPENDENCIES (best-effort, degrades gracefully if absent)
#   nvidia-smi  — GPU metrics; absent → GPU fields written as null
#   curl        — Ollama API + /api/tags; absent → vram_loaded written as null
#   jq          — JSON serialisation; absent → hand-built JSON line (documented)
#
# CONTRACT
#   Never fails the build. Exits 0 even when all optional tools are absent.
#   A degraded run writes run-metadata.json noting which tools were missing.
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${OUT_DIR:-${REPO_ROOT}/artifacts/perf/${TIMESTAMP}}"
mkdir -p "${OUT_DIR}"

# Fail loud, not silent-0-line: if OUT_DIR is not writable (read-only FS,
# wrong owner, SELinux), every >> append would die under `set -e` with an
# opaque message. Probe-write a sentinel and exit 0 cleanly (the probe is
# non-fatal to the bench; the bench records absent gpu-timeseries as a caveat).
if ! ( : > "${OUT_DIR}/.gpu_probe.wtest" ) 2>/dev/null; then
  echo "[gpu_probe] WARN: OUT_DIR not writable (${OUT_DIR}) — GPU probe disabled" >&2
  exit 0
fi
rm -f "${OUT_DIR}/.gpu_probe.wtest" 2>/dev/null || true

# Named constant for polling interval (env override supported).
PERF_GPU_POLL_SECONDS="${PERF_GPU_POLL_SECONDS:-2}"

# Sentinel file path (optional — also responds to SIGTERM).
PERF_GPU_PROBE_STOP="${PERF_GPU_PROBE_STOP:-}"

OLLAMA_HOST_PORT="${OLLAMA_HOST_PORT:-11434}"
OLLAMA_API_BASE="http://localhost:${OLLAMA_HOST_PORT}"

LITELLM_CONFIG="${REPO_ROOT}/litellm/config.yaml"

# Compose-defined defaults (documented here so readers know what "unset" means).
#   OLLAMA_MAX_LOADED_MODELS: compose sets 3 via ${OLLAMA_MAX_LOADED_MODELS:-3}
#   OLLAMA_NUM_PARALLEL:      compose hard-sets 2
OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-3}"
OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-2}"

log() { echo "[gpu_probe] $*" >&2; }

# ---------------------------------------------------------------------------
# Tool availability guards
# ---------------------------------------------------------------------------
HAS_NVIDIA_SMI=0
HAS_CURL=0
HAS_JQ=0
command -v nvidia-smi >/dev/null 2>&1 && HAS_NVIDIA_SMI=1
command -v curl       >/dev/null 2>&1 && HAS_CURL=1
command -v jq         >/dev/null 2>&1 && HAS_JQ=1

MISSING_TOOLS=()
[[ "${HAS_NVIDIA_SMI}" -eq 0 ]] && MISSING_TOOLS+=("nvidia-smi")
[[ "${HAS_CURL}"       -eq 0 ]] && MISSING_TOOLS+=("curl")

if [[ "${#MISSING_TOOLS[@]}" -gt 0 ]]; then
  log "WARN: missing tools: ${MISSING_TOOLS[*]} — affected fields will be null in output"
fi

# ---------------------------------------------------------------------------
# Helper: escape a string for JSON (no external deps)
# ---------------------------------------------------------------------------
json_escape() {
  # Escapes: backslash (\), double-quote ("), tab (\t), carriage-return (\r),
  # and newline (\n) — the five characters requiring JSON escaping in string values.
  # Handles the common cases for model names, paths, and git hashes.
  printf '%s' "$1" \
    | sed 's/\\/\\\\/g; s/"/\\"/g; s/	/\\t/g; s/\r/\\r/g; s/\n/\\n/g'
}

# ---------------------------------------------------------------------------
# Helper: emit a JSON null or quoted string
# ---------------------------------------------------------------------------
json_str_or_null() {
  local val="$1"
  if [[ -z "${val}" ]]; then
    printf 'null'
  else
    printf '"%s"' "$(json_escape "${val}")"
  fi
}

# ---------------------------------------------------------------------------
# Helper: emit a JSON null or unquoted number
# ---------------------------------------------------------------------------
json_num_or_null() {
  local val="$1"
  # Empty OR non-numeric (e.g. nvidia-smi "N/A" / "[Not Supported]" on some
  # driver/GPU combos) → JSON null, never a raw token that breaks the line.
  if [[ -z "${val}" || ! "${val}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    printf 'null'
  else
    printf '%s' "${val}"
  fi
}

# ---------------------------------------------------------------------------
# Parse LiteLLM alias→model map from litellm/config.yaml at runtime.
# Reads only active (uncommented) model_name / model: pairs.
# Returns a JSON object string: {"smart":"ollama/qwen3:8b", ...}
# Pure awk — no python, no jq required.
# ---------------------------------------------------------------------------
parse_litellm_aliases() {
  if [[ ! -f "${LITELLM_CONFIG}" ]]; then
    log "WARN: ${LITELLM_CONFIG} not found — alias map will be empty"
    printf '{}'
    return
  fi

  awk '
    # Track indent depth of model_list items so we pick the *active* entries.
    # We look for lines matching:
    #   "  - model_name: \"<alias>\""   (under model_list)
    #   "      model: \"<provider/name>\""  (under litellm_params)
    # Commented lines (leading #) are excluded.

    /^[[:space:]]*#/ { next }   # skip comment lines

    /^[[:space:]]*-[[:space:]]+model_name:/ {
      match($0, /model_name:[[:space:]]*"?([^"[:space:]]+)"?/, arr)
      if (RLENGTH > 0) {
        current_alias = arr[1]
        # Remove surrounding quotes if any
        gsub(/"/, "", current_alias)
      }
      next
    }

    /^[[:space:]]+model:[[:space:]]/ {
      if (current_alias != "") {
        match($0, /model:[[:space:]]*"?([^"[:space:]]+)"?/, arr)
        if (RLENGTH > 0) {
          model_val = arr[1]
          gsub(/"/, "", model_val)
          # Store only the FIRST active entry per alias
          if (!(current_alias in seen)) {
            seen[current_alias] = model_val
            aliases[current_alias] = model_val
          }
          current_alias = ""
        }
      }
      next
    }

    END {
      printf "{"
      first = 1
      for (a in aliases) {
        if (!first) printf ","
        # Escape backslash then double-quote in both key and value before emitting
        key = a;   gsub(/\\/, "\\\\", key);   gsub(/"/, "\\\"", key)
        val = aliases[a]; gsub(/\\/, "\\\\", val); gsub(/"/, "\\\"", val)
        printf "\"%s\":\"%s\"", key, val
        first = 0
      }
      printf "}"
    }
  ' "${LITELLM_CONFIG}"
}

# ---------------------------------------------------------------------------
# Collect one nvidia-smi sample.
# Returns: gpu_name, vram_total_mb, vram_used_mb, gpu_util_pct (all or null)
# ---------------------------------------------------------------------------
sample_nvidia_smi() {
  if [[ "${HAS_NVIDIA_SMI}" -eq 0 ]]; then
    SAMPLE_GPU_NAME=""
    SAMPLE_VRAM_TOTAL=""
    SAMPLE_VRAM_USED=""
    SAMPLE_GPU_UTIL=""
    SAMPLE_GPU_COUNT=""
    return
  fi

  # Multi-GPU honesty: we only sample GPU 0 (head -n1 below). Record the count
  # so a bundle from a box where load lands on GPU≠0 is detectable, not
  # silently mismeasured.
  SAMPLE_GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -c . || echo "")

  local raw
  # Query first GPU only (index 0). Fields: name,memory.total,memory.used,utilization.gpu
  # --format=csv,noheader,nounits → values only, no headers, no "MiB"/"%"
  raw=$(nvidia-smi \
    --query-gpu=name,memory.total,memory.used,utilization.gpu \
    --format=csv,noheader,nounits \
    2>/dev/null | head -n1 || true)

  if [[ -z "${raw}" ]]; then
    SAMPLE_GPU_NAME=""
    SAMPLE_VRAM_TOTAL=""
    SAMPLE_VRAM_USED=""
    SAMPLE_GPU_UTIL=""
    return
  fi

  # Parse CSV: "NVIDIA GeForce RTX 5060 Ti, 16376, 4096, 37"
  SAMPLE_GPU_NAME=$(printf '%s' "${raw}" | awk -F',' '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1); print $1}')
  SAMPLE_VRAM_TOTAL=$(printf '%s' "${raw}" | awk -F',' '{gsub(/[[:space:]]/, "", $2); print $2}')
  SAMPLE_VRAM_USED=$(printf '%s' "${raw}" | awk -F',' '{gsub(/[[:space:]]/, "", $3); print $3}')
  SAMPLE_GPU_UTIL=$(printf '%s' "${raw}" | awk -F',' '{gsub(/[[:space:]]/, "", $4); print $4}')
}

# ---------------------------------------------------------------------------
# Fetch Ollama /api/tags and extract per-model size_vram (bytes).
# Returns: JSON object string {"model_name": size_vram_bytes, ...} or {}
# ---------------------------------------------------------------------------
fetch_ollama_vram() {
  if [[ "${HAS_CURL}" -eq 0 ]]; then
    printf '{}'
    return
  fi

  local tags_json
  tags_json=$(curl -sf --max-time 5 \
    "${OLLAMA_API_BASE}/api/tags" 2>/dev/null || true)

  if [[ -z "${tags_json}" ]]; then
    printf '{}'
    return
  fi

  if [[ "${HAS_JQ}" -eq 1 ]]; then
    # jq path: robust, handles all edge cases
    printf '%s' "${tags_json}" | jq -c '
      .models // [] |
      map(select(.name != null)) |
      map({ (.name): (.size_vram // null) }) |
      add // {}
    ' 2>/dev/null || printf '{}'
  else
    # Fallback: awk-based extraction for {"name":"...", ..., "size_vram":N}
    # Works for typical Ollama /api/tags flat JSON response.
    printf '%s' "${tags_json}" | awk '
      BEGIN { RS=",|{|}" ; printf "{"; first=1 }
      /"name"[[:space:]]*:/ {
        match($0, /"name"[[:space:]]*:[[:space:]]*"([^"]+)"/, arr)
        if (RLENGTH > 0) current_name = arr[1]
      }
      /"size_vram"[[:space:]]*:/ {
        match($0, /"size_vram"[[:space:]]*:[[:space:]]*([0-9]+)/, arr)
        if (RLENGTH > 0 && current_name != "") {
          if (!first) printf ","
          printf "\"%s\":%s", current_name, arr[1]
          first=0
          current_name=""
        }
      }
      END { printf "}" }
    ' 2>/dev/null || printf '{}'
  fi
}

# ---------------------------------------------------------------------------
# Emit one timeseries line to gpu-timeseries.jsonl
# ---------------------------------------------------------------------------
TIMESERIES_FILE="${OUT_DIR}/gpu-timeseries.jsonl"

emit_timeseries_line() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  sample_nvidia_smi
  local vram_loaded
  vram_loaded="$(fetch_ollama_vram)"

  if [[ "${HAS_JQ}" -eq 1 ]]; then
    jq -cn \
      --arg ts "${ts}" \
      --argjson gpu_name "$(json_str_or_null "${SAMPLE_GPU_NAME}")" \
      --argjson vram_total "$(json_num_or_null "${SAMPLE_VRAM_TOTAL}")" \
      --argjson vram_used "$(json_num_or_null "${SAMPLE_VRAM_USED}")" \
      --argjson gpu_util "$(json_num_or_null "${SAMPLE_GPU_UTIL}")" \
      --argjson vram_loaded "${vram_loaded}" \
      '{ts: $ts, gpu_name: $gpu_name, vram_total_mb: $vram_total,
        vram_used_mb: $vram_used, gpu_util_pct: $gpu_util,
        vram_loaded_bytes: $vram_loaded}' \
      >> "${TIMESERIES_FILE}"
  else
    # Hand-built JSON line (no jq).
    # Fields: ts, gpu_name, vram_total_mb, vram_used_mb, gpu_util_pct, vram_loaded_bytes
    printf '{"ts":"%s","gpu_name":%s,"vram_total_mb":%s,"vram_used_mb":%s,"gpu_util_pct":%s,"vram_loaded_bytes":%s}\n' \
      "$(json_escape "${ts}")" \
      "$(json_str_or_null "${SAMPLE_GPU_NAME}")" \
      "$(json_num_or_null "${SAMPLE_VRAM_TOTAL}")" \
      "$(json_num_or_null "${SAMPLE_VRAM_USED}")" \
      "$(json_num_or_null "${SAMPLE_GPU_UTIL}")" \
      "${vram_loaded}" \
      >> "${TIMESERIES_FILE}"
  fi
}

# ---------------------------------------------------------------------------
# Emit run-metadata.json (once, before polling loop)
# ---------------------------------------------------------------------------
emit_run_metadata() {
  local meta_file="${OUT_DIR}/run-metadata.json"

  # GPU static fields (from first sample)
  sample_nvidia_smi

  # LiteLLM alias map (parsed from litellm/config.yaml — never hardcoded)
  local alias_map
  alias_map="$(parse_litellm_aliases)"

  # Git commit
  local git_commit=""
  git_commit="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"

  # UTC timestamp
  local run_ts
  run_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # PERF_CONCURRENCY: may be unset → null
  local concurrency_val="${PERF_CONCURRENCY:-}"

  # Missing-tools note
  local degraded_note=""
  if [[ "${#MISSING_TOOLS[@]}" -gt 0 ]]; then
    degraded_note="Degraded capture — missing: ${MISSING_TOOLS[*]}"
  fi

  if [[ "${HAS_JQ}" -eq 1 ]]; then
    jq -n \
      --arg gpu_name "${SAMPLE_GPU_NAME:-}" \
      --argjson gpu_count "$(json_num_or_null "${SAMPLE_GPU_COUNT:-}")" \
      --argjson vram_total_mb "$(json_num_or_null "${SAMPLE_VRAM_TOTAL}")" \
      --argjson vram_used_mb_at_start "$(json_num_or_null "${SAMPLE_VRAM_USED}")" \
      --argjson perf_concurrency "$(json_num_or_null "${concurrency_val}")" \
      --argjson litellm_aliases "${alias_map}" \
      --arg ollama_max_loaded_models "${OLLAMA_MAX_LOADED_MODELS}" \
      --arg ollama_num_parallel "${OLLAMA_NUM_PARALLEL}" \
      --arg git_commit "${git_commit}" \
      --arg timestamp "${run_ts}" \
      --arg degraded_note "${degraded_note}" \
      '{
        gpu_name: (if $gpu_name == "" then null else $gpu_name end),
        gpu_count: $gpu_count,
        vram_total_mb: $vram_total_mb,
        vram_used_mb_at_start: $vram_used_mb_at_start,
        perf_concurrency: $perf_concurrency,
        litellm_aliases: $litellm_aliases,
        ollama_max_loaded_models: ($ollama_max_loaded_models | tonumber),
        ollama_num_parallel: ($ollama_num_parallel | tonumber),
        git_commit: (if $git_commit == "" then null else $git_commit end),
        timestamp_utc: $timestamp,
        degraded_note: (if $degraded_note == "" then null else $degraded_note end)
      }' > "${meta_file}"
  else
    # Hand-built JSON (no jq).
    {
      printf '{\n'
      printf '  "gpu_name": %s,\n'                  "$(json_str_or_null "${SAMPLE_GPU_NAME}")"
      printf '  "gpu_count": %s,\n'                 "$(json_num_or_null "${SAMPLE_GPU_COUNT:-}")"
      printf '  "vram_total_mb": %s,\n'              "$(json_num_or_null "${SAMPLE_VRAM_TOTAL}")"
      printf '  "vram_used_mb_at_start": %s,\n'      "$(json_num_or_null "${SAMPLE_VRAM_USED}")"
      printf '  "perf_concurrency": %s,\n'           "$(json_num_or_null "${concurrency_val}")"
      printf '  "litellm_aliases": %s,\n'            "${alias_map}"
      printf '  "ollama_max_loaded_models": %s,\n'   "$(json_num_or_null "${OLLAMA_MAX_LOADED_MODELS}")"
      printf '  "ollama_num_parallel": %s,\n'        "$(json_num_or_null "${OLLAMA_NUM_PARALLEL}")"
      printf '  "git_commit": %s,\n'                 "$(json_str_or_null "${git_commit}")"
      printf '  "timestamp_utc": %s,\n'              "$(json_str_or_null "${run_ts}")"
      printf '  "degraded_note": %s\n'               "$(json_str_or_null "${degraded_note}")"
      printf '}\n'
    } > "${meta_file}"
  fi

  log "run-metadata.json → ${meta_file}"
}

# ---------------------------------------------------------------------------
# Signal / sentinel handling for clean shutdown
# ---------------------------------------------------------------------------
STOP_REQUESTED=0

handle_term() {
  log "SIGTERM received — stopping poll loop"
  STOP_REQUESTED=1
}
trap 'handle_term' TERM INT

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
log "Starting GPU probe (poll every ${PERF_GPU_POLL_SECONDS}s)"
log "Output dir: ${OUT_DIR}"
[[ -n "${PERF_GPU_PROBE_STOP}" ]] && log "Sentinel stop file: ${PERF_GPU_PROBE_STOP}"

# Emit metadata once before polling
emit_run_metadata

log "Polling → ${TIMESERIES_FILE}"

while true; do
  # Check sentinel file
  if [[ -n "${PERF_GPU_PROBE_STOP}" ]] && [[ -f "${PERF_GPU_PROBE_STOP}" ]]; then
    log "Sentinel file found (${PERF_GPU_PROBE_STOP}) — stopping"
    break
  fi

  # Check signal flag
  if [[ "${STOP_REQUESTED}" -eq 1 ]]; then
    break
  fi

  emit_timeseries_line

  # Sleep in 1-second ticks so SIGTERM / sentinel are checked promptly.
  # PERF_GPU_POLL_SECONDS may be a float (e.g. 2.5); bash arithmetic rejects
  # floats, so we strip the decimal part before the calculation (truncates, does
  # not round).  An empty or non-numeric value also falls back to 1.
  _poll_int="${PERF_GPU_POLL_SECONDS%%.*}"
  [[ "${_poll_int}" =~ ^[0-9]+$ ]] || _poll_int="1"
  local_ticks=$(( _poll_int > 0 ? _poll_int : 1 ))
  local_i=0
  while (( local_i < local_ticks )); do
    sleep 1 || true
    (( local_i++ )) || true
    if [[ "${STOP_REQUESTED}" -eq 1 ]]; then break; fi
    if [[ -n "${PERF_GPU_PROBE_STOP}" ]] && [[ -f "${PERF_GPU_PROBE_STOP}" ]]; then break; fi
  done
done

log "GPU probe stopped. Timeseries lines: $(wc -l < "${TIMESERIES_FILE}" 2>/dev/null || echo 0)"
