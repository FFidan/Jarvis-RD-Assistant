#!/usr/bin/env bash
# scripts/perf/_bench_lib.sh — shared helpers for vllm_confirmatory_bench.sh.
#
# This is a *sourced* library, not an entrypoint. It deliberately does NOT set
# `set -euo pipefail` (the caller owns shell options) and defines only
# functions + a couple of readonly defaults.
#
# Design notes
#   - The confirmatory bench has a DIFFERENT contract from loadgen.sh/profile.sh:
#     those degrade to exit 0; this bench HARD-ABORTS on a precondition failure
#     so it never silently produces a meaningless result on a box the agent
#     cannot see. `bench_die` records the reason into the bundle and returns a
#     non-zero status so the caller can still tar a partial+diagnostic bundle.
#   - The LiteLLM `smart` alias is the only call-site seam (litellm/config.yaml).
#     Mutation is transient; restore is always `git checkout --` of the tracked,
#     pristine file (deterministic — never best-effort sed-back).
# =============================================================================

# --- logging --------------------------------------------------------------
bench_log()  { echo "[bench] $*" >&2; }
bench_warn() { echo "[bench][WARN] $*" >&2; }

# bench_die <reason...> : append the reason to the bundle's ABORT.txt (if the
# caller exported BENCH_OUT_DIR) and return 1. The caller is responsible for
# bundling + exiting; this function never calls `exit` so the trap can still
# emit a diagnostic tarball.
bench_die() {
  local msg="$*"
  bench_warn "ABORT: ${msg}"
  if [[ -n "${BENCH_OUT_DIR:-}" ]]; then
    mkdir -p "${BENCH_OUT_DIR}" 2>/dev/null || true
    {
      echo "ABORT $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "${msg}"
    } >> "${BENCH_OUT_DIR}/ABORT.txt"
  fi
  return 1
}

# --- perf-probe truncate / collect ----------------------------------------
# Mirrors scripts/profile.sh:58-63 (truncate) and :237-245 (collect). The
# perf-probe.jsonl is written container-side at /data/perf via the
# docker-compose.perf.yml bind-mount of ${REPO_ROOT}/shared/perf.
probe_host_file() { echo "${BENCH_REPO_ROOT}/shared/perf/perf-probe.jsonl"; }

probe_truncate() {
  local d
  d="$(dirname "$(probe_host_file)")"
  mkdir -p "${d}"
  chmod 777 "${d}" 2>/dev/null || true
  : > "$(probe_host_file)" 2>/dev/null || true
  chmod 666 "$(probe_host_file)" 2>/dev/null || true
}

# probe_collect <dest_jsonl> : copy the container-written spans into the
# per-sweep artifact dir. Returns 1 (non-fatal to caller) if empty so the
# caller can flag a stale-image / probe-disabled run loudly.
probe_collect() {
  local dest="$1" src
  src="$(probe_host_file)"
  if [[ -s "${src}" ]]; then
    cp "${src}" "${dest}"
    bench_log "collected $(wc -l < "${dest}" | tr -d ' ') probe span(s) → ${dest}"
    return 0
  fi
  bench_warn "perf-probe.jsonl empty — stale image or probes not armed (HOWTO Caveat 1)"
  return 1
}

# --- LiteLLM smart-alias seam --------------------------------------------
# All mutations start from the tracked pristine file (git show HEAD:) so the
# per-pair model substitution is deterministic regardless of what the working
# copy currently contains. python3 is a hard preflight dependency.
_litellm_path() { echo "${BENCH_REPO_ROOT}/litellm/config.yaml"; }

# _litellm_rewrite_smart <engine> <model> : regenerate config.yaml with the
# single active `smart` entry pointed at <engine>/<model>. Replaces the
# contiguous active smart block (first uncommented `- model_name: "smart"` up
# to the next `# --- ` divider or next uncommented `- model_name:`).
_litellm_rewrite_smart() {
  local engine="$1" model="$2"
  python3 - "$(_litellm_path)" "${engine}" "${model}" <<'PY'
import sys, subprocess, re
cfg_path, engine, model = sys.argv[1], sys.argv[2], sys.argv[3]
# Pristine baseline from git (HEAD), never the possibly-mutated working copy.
pristine = subprocess.check_output(
    ["git", "-C", __import__("os").path.dirname(cfg_path) + "/..",
     "show", "HEAD:litellm/config.yaml"], text=True
).splitlines()

start = None
for i, ln in enumerate(pristine):
    if re.match(r'^\s*-\s+model_name:\s*"smart"\s*$', ln) and not ln.lstrip().startswith("#"):
        start = i
        break
if start is None:
    sys.exit("FATAL: no active smart block in pristine config")

end = len(pristine)
for j in range(start + 1, len(pristine)):
    s = pristine[j]
    if re.match(r'^\s*#\s*---\s', s) or (
        re.match(r'^\s*-\s+model_name:', s) and not s.lstrip().startswith("#")
    ):
        end = j
        break

if engine == "vllm":
    block = [
        '  - model_name: "smart"',
        '    litellm_params:',
        f'      model: "openai/{model}"',
        '      api_base: "http://vllm:8080/v1"',
        '      api_key: "vllm-no-key"',
        '      temperature: 0.2',
        '      max_tokens: 4096',
        '',
    ]
elif engine == "ollama":
    block = [
        '  - model_name: "smart"',
        '    litellm_params:',
        f'      model: "ollama/{model}"',
        '      api_base: "http://ollama:11434"',
        '      temperature: 0.2',
        '      num_ctx: 8192',
        '      extra_body:',
        '        think: false',
        '',
    ]
else:
    sys.exit(f"FATAL: unknown engine {engine}")

out = pristine[:start] + block + pristine[end:]
with open(cfg_path, "w") as fh:
    fh.write("\n".join(out) + "\n")
print(f"litellm smart -> {engine}/{model}")
PY
}

litellm_smart_to_vllm()   { _litellm_rewrite_smart vllm   "$1"; }
litellm_smart_to_ollama() { _litellm_rewrite_smart ollama "$1"; }

# Deterministic restore. Primary: git checkout. Fallback (dirty index / a
# `git checkout --` failure): write HEAD's content directly — same pristine
# baseline _litellm_rewrite_smart builds from, never touches the index, so it
# cannot clobber unrelated staged changes. A left-mutated config is a
# measurement-validity hazard, so the post-condition is asserted loudly.
litellm_restore() {
  local cfg; cfg="$(_litellm_path)"
  if git -C "${BENCH_REPO_ROOT}" checkout -- litellm/config.yaml 2>/dev/null; then
    bench_log "litellm/config.yaml restored (git checkout --)"
  elif git -C "${BENCH_REPO_ROOT}" show HEAD:litellm/config.yaml > "${cfg}" 2>/dev/null; then
    bench_log "litellm/config.yaml restored (git show HEAD: fallback — index untouched)"
  else
    bench_die "litellm/config.yaml restore FAILED (git checkout AND HEAD-show) — config left MUTATED; bench validity compromised"
    return 1
  fi
  # Post-condition: exactly one uncommented active `smart` entry.
  local n
  n="$(grep -cE '^[[:space:]]*-[[:space:]]+model_name:[[:space:]]*"smart"[[:space:]]*$' "${cfg}" 2>/dev/null || echo 0)"
  if [[ "${n}" != "1" ]]; then
    bench_die "litellm/config.yaml restore post-check: expected 1 active 'smart' block, found ${n} — config left MUTATED"
    return 1
  fi
}

# --- vLLM routing proof ---------------------------------------------------
# vLLM exposes Prometheus metrics at /metrics. `vllm:request_success_total`
# is a monotonic counter; a positive delta across a sweep proves the smart
# alias actually routed to vLLM (not Ollama). Absent metrics → empty string,
# caller treats as inconclusive (warn, not silent-pass).
vllm_success_total() {
  local port="${VLLM_HOST_PORT:-8080}"
  # Explicit matched-flag (portable across gawk/mawk uninitialized-var
  # semantics): counter ABSENT → "" (inconclusive, caller warns, never
  # silent-pass); counter present (even 0) → the integer.
  curl -sf --max-time 5 "http://localhost:${port}/metrics" 2>/dev/null \
    | awk '/^vllm:request_success_total/ {s+=$NF; seen=1}
           END {if (seen) printf "%d", s}'
}

# --- /api/ask SSE drain (quality capture) ---------------------------------
# /api/ask returns media_type text/event-stream (analyze/rag StreamingResponse).
# Events: `event: token|sources|done|error` + `data: <payload>` lines, with a
# [DONE] terminator. We concatenate the token-event data payloads into the
# full answer text. Best-effort: any failure writes an explanatory stub so the
# DIFF.md still has something to compare.
sse_drain() {
  local base="$1" cookie="$2" body="$3" out="$4"
  local raw nobuf="-N"
  # curl <7.68 silently ignores -N and buffers the whole stream → the
  # SSE_MAX_SECONDS timeout fires before the answer returns. Detect + warn.
  local cv; cv="$(curl --version 2>/dev/null | awk 'NR==1{print $2}')"
  if [[ -n "${cv}" ]]; then
    local cmaj cmin; cmaj="${cv%%.*}"; cmin="${cv#*.}"; cmin="${cmin%%.*}"
    if [[ "${cmaj}" =~ ^[0-9]+$ && "${cmin}" =~ ^[0-9]+$ ]] \
       && { (( cmaj < 7 )) || { (( cmaj == 7 )) && (( cmin < 68 )); }; }; then
      nobuf=""
      bench_warn "curl ${cv} < 7.68: -N unsupported, SSE may buffer (quality capture only)"
    fi
  fi
  raw="$(curl -s ${nobuf} --max-time "${SSE_MAX_SECONDS:-300}" \
            -X POST "${base}/api/ask" \
            -b "${cookie}" \
            -H "Content-Type: application/json" \
            -H "Accept: text/event-stream" \
            -d "${body}" 2>/dev/null || true)"
  if [[ -z "${raw}" ]]; then
    echo "[sse_drain: empty response — endpoint unreachable or 500]" > "${out}"
    return 1
  fi
  # /api/ask is NOT always SSE: the decompose path returns a single JSON body
  # {"answer": "...","sources":[...]}. Try SSE `data:` frames first; if none,
  # fall back to extracting .answer from a JSON body so the quality DIFF (and
  # the manual sign-off gate) is actually populated instead of the useless
  # "[sse_drain: no data events parsed]" placeholder.
  printf '%s\n' "${raw}" \
    | sed -n 's/^data: \{0,1\}//p' \
    | grep -v '^\[DONE\]$' \
    > "${out}" || true
  if [[ ! -s "${out}" ]]; then
    printf '%s' "${raw}" | python3 -c '
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    sys.stdout.write(raw.strip()[:8000]); sys.exit(0)
ans = d.get("answer")
if ans:
    src = d.get("sources") or []
    sys.stdout.write(ans.rstrip() + "\n\n[sources: %d]\n" % len(src))
else:
    sys.stdout.write(json.dumps(d)[:8000])
' > "${out}" 2>/dev/null || true
  fi
  [[ -s "${out}" ]] || echo "[sse_drain: empty/unparseable answer body]" > "${out}"
}

# --- scenario_c_rag_ask extractor ----------------------------------------
# loadgen-summary.csv columns: scenario,requests,p50_s,p95_s,p99_s,throughput_rps
# Echoes "p50 p95" (seconds) for the scenario_c_rag_ask row, or "NA NA".
rag_ask_p50_p95() {
  local csv="$1"
  [[ -f "${csv}" ]] || { echo "NA NA"; return 1; }
  awk -F',' '$1=="scenario_c_rag_ask" {print $3, $4; found=1}
             END {if (!found) print "NA NA"}' "${csv}"
}
