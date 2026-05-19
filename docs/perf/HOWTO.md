# Performance Profiling — HOWTO

<!-- (agent: claude-code) Last updated 2026-05-18 for Task B1-4 (loadgen + gpu_probe wiring) -->

`make profile` runs `scripts/profile.sh` end-to-end, dumping a snapshot under
`artifacts/perf/<UTC-timestamp>/`. The harness is deliberately best-effort:
each step degrades to a logged warning if its tool is missing, so a partial
snapshot still ships.

## Tool prerequisites

| Capture | Tool | Install | Notes |
|---|---|---|---|
| Frontend bundle sizes | `npm run build` | `cd frontend && npm install --legacy-peer-deps` | Always runs. |
| Backend GET timings | `curl` | system-provided | Reads API key from `secrets/jarvis_api_key.txt`. |
| py-spy flamegraph | `py-spy` | `pipx install py-spy` or `uv tool install py-spy` | **Linux usually requires sudo** to ptrace the uvicorn PID. To grant py-spy non-sudo access: `sudo setcap cap_sys_ptrace=eip $(which py-spy)`. |
| pg_stat_statements top-N | Postgres ext | see below | Requires a server restart to preload. |
| Lighthouse | `npx lighthouse` | downloads on first run | Needs Chrome; falls back gracefully. |
| GPU/VRAM telemetry | `nvidia-smi` | driver-provided | Degrades to null fields when absent. |
| Concurrency load | `bash` + `curl` | system-provided | Pure shell — no extra install. |

## Enabling pg_stat_statements

`pg_stat_statements` requires the extension to be loaded via
`shared_preload_libraries`. The default Postgres image used here does not
preload it. To enable:

```bash
# 1. Add to docker-compose Postgres command, or via init script:
docker exec jarvis_rd_assistant-postgres-1 psql -U jarvis -d jarvis -c \
  "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';"

# 2. Restart Postgres:
docker restart jarvis_rd_assistant-postgres-1

# 3. Create the extension in the target DB:
docker exec jarvis_rd_assistant-postgres-1 psql -U jarvis -d jarvis -c \
  "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"

# 4. Re-run the workload, then:
make profile
```

The 2026-05-10 baseline run did NOT include `pg_stat_statements` data because
the extension was not preloaded. Sub-10 ms wall-clock on every measured GET
endpoint suggests Python is not the bottleneck on the read path; LLM-bound
endpoints (Pulse generate, RAG chat) were not measured because they require
Ollama warm-up plus a logged-in user session.

## Environment knobs

All knobs are optional. Sensible defaults are shown.

| Variable | Default | Description |
|---|---|---|
| `PERF_CONCURRENCY` | `10` | Concurrent curl workers per loadgen batch. Lower (e.g. `3`) for CI; raise for stress testing. |
| `PERF_PROBE_ENABLED` | `0` | Set to `1` to enable in-process span probes (pulse Stage-2 LLM, embed POST, BM25 SQL, cross-paper RAG). **Must be set in the Docker container env** (not just the host shell) — see "Enabling in-process probes" below. |
| `PERF_PROBE_PATH` | `${OUT_DIR}/perf-probe.jsonl` | Override probe output path (usually leave at default). |
| `PERF_GPU_POLL_SECONDS` | `2` | `gpu_probe.sh` polling interval in seconds (float allowed, e.g. `0.5` for high-res). |
| `SKIP_LIGHTHOUSE` | `0` | Set to `1` to skip Lighthouse (useful on CI without Chrome). |
| `PAPER_INGESTION_HOST_PORT` | `8010` | Override the backend port. |

## Running it

```bash
# Optional: restart local services with profiling-only compose overrides.
# This preloads pg_stat_statements and grants SYS_PTRACE to paper_ingestion.
make profile-stack-up

# Optional: also start the Vector log-aggregation sidecar (mounts docker.sock).
# Requires explicit opt-in because it carries privileged docker-socket access.
docker compose --profile observability up -d vector

# Default: full snapshot
make profile

# With concurrency knobs:
PERF_CONCURRENCY=20 PERF_PROBE_ENABLED=1 make profile

# Frontend bundle treemap (writes frontend/dist/bundle-stats.html):
ANALYZE_BUNDLE=true npm --prefix frontend run build

# Skip Lighthouse (often fails on CI without Chrome):
SKIP_LIGHTHOUSE=1 make profile

# Override default port:
PAPER_INGESTION_HOST_PORT=8010 make profile
```

Outputs:

- `frontend-bundle-sizes.txt` — `ls -lh dist/assets/*.js | sort -k5 -h`
- `frontend/dist/bundle-stats.html` — optional Vite/Rollup treemap when
  `ANALYZE_BUNDLE=true`
- `backend-timings.csv` — per-endpoint wall-clock (3 runs)
- `flamegraph.svg` — py-spy 30s record (when granted ptrace)
- `pg-stat-statements-top20.csv` — when extension is preloaded
- `lighthouse.html` — when Chrome is available
- `run-metadata.json` — GPU model, VRAM total/used at start, LiteLLM alias map,
  `PERF_CONCURRENCY`, `OLLAMA_MAX_LOADED_MODELS`/`OLLAMA_NUM_PARALLEL`, git commit, UTC timestamp
- `gpu-timeseries.jsonl` — one JSON-lines record per `PERF_GPU_POLL_SECONDS`; fields:
  `ts`, `gpu_name`, `vram_total_mb`, `vram_used_mb`, `gpu_util_pct`, `vram_loaded_bytes`
- `loadgen-concurrency.csv` — per-request latencies for Scenario A (fan-out) + B (sustained)
- `loadgen-summary.csv` — p50/p95/p99 + throughput (req/s) per scenario
- `perf-probe.jsonl` — in-process span records (non-empty only when `PERF_PROBE_ENABLED=1` is
  set in the container environment and the container was recreated with that flag)

The profiling override lives in `docker-compose.perf.yml` and is intentionally
not loaded by normal `make up`, `make up-build`, or production compose usage.
It changes local developer observability only; it must not become part of the
default runtime security posture.

## Enabling in-process probes (`PERF_PROBE_ENABLED`)

The `perf_probe.py` module inside `paper_ingestion` reads `PERF_PROBE_ENABLED` from its own
process environment at import time. Setting it in the host shell only passes it to the
`loadgen.sh` child process; the running Docker container is unaffected.

`docker-compose.perf.yml` now bakes this in: `paper_ingestion` gets
`PERF_PROBE_ENABLED=1` + `PERF_PROBE_PATH=/data/perf/perf-probe.jsonl` and a
`./shared/perf:/data/perf` bind-mount; `profile.sh` truncates that file per-run
and collects it into the artifact dir. So the capture path is:

```bash
# 1. REBUILD from current source first (MANDATORY — see caveat below):
docker compose -f docker-compose.yml build paper_ingestion learning_engine dashboard

# 2. Boot with the perf override (probes on, mount wired):
make profile-stack-up   # uses docker-compose.perf.yml

# 3. Run the profile:
make profile
# → artifacts/perf/<ts>/perf-probe.jsonl
```

The probe writes `{"span": "<name>", "ms": …, "ts": …, …}` JSONL lines.

**Observer-effect caveat — synchronous writes on the asyncio event loop.** `perf_probe.py`
appends JSONL records synchronously inside `__exit__`, which blocks the event loop for the
duration of the file write. This is acceptable for profiling runs where the probe is the point,
but it is NOT production-safe — do not enable `PERF_PROBE_ENABLED=1` in normal deployments.
Additionally, `learning_engine` LLM paths (FSRS scheduling, card generation) are **not**
instrumented by `perf_probe` (SYM-02) — only `paper_ingestion` hot paths are wired. Cross-service
bottleneck analysis requires separate instrumentation (e.g. Langfuse traces).

**Caveat 1 — rebuild or you get nothing.** The deployed `jarvis/paper_ingestion`
image is a pinned tag, not a live bind-mount of the source. If it was built
before the probe code (or any code you want to measure) landed, the running
container has no probes and the JSONL is silently empty. Always rebuild the app
images from the commit under test before profiling.

**loadgen Scenario C drives the probed paths (since 2026-05-17).**
`loadgen.sh` Scenario C mints a real owner session via `POST
/api/auth/api-key-session` (exchanges the existing `JARVIS_API_KEY` for a
`jarvis_session` cookie — no email/magic-link) and drives all four wired
spans: `POST /api/papers/search-hybrid` (→ `embed_texts_post` +
`hybrid_search_bm25_sql`), `POST /api/ask` decompose (→
`prepare_cross_paper_rag` + `embed_texts_post`), `POST /api/pulse/generate`
(→ `pulse_stage2_llm`, async worker). With probes armed it now collects real
spans (verified: 54 spans, evidence-based bottleneck recorded in
`docs/perf/2026-05-18-baseline-report.md` §"Dominant bottleneck DETERMINED").

Preconditions / knobs:
- Session mint needs **exactly one non-deleted user + an admin**, or
  `API_KEY_LOGIN_ENABLED=true` (else Scenario C logs a warning and skips —
  exit 0 preserved).
- A **seeded corpus** makes `pulse_stage2_llm`/`prepare_cross_paper_rag`
  absolute numbers meaningful; with an empty corpus they still emit (floors)
  and `embed_texts_post`/`hybrid_search_bm25_sql` are corpus-independent.
- `RAG_CONCURRENCY` (default ≤3) bounds the heavy `/api/ask` fan-out;
  `PERF_PULSE_SETTLE_SECS` (default 25) lets the async Pulse worker emit its
  span before `profile.sh` collects the JSONL.

## Re-running unchanged on the 48 GB box

No constants to edit — all machine values (GPU model, VRAM, LiteLLM alias map, concurrency)
are captured automatically into `run-metadata.json`. To reproduce on a 48 GB GPU host:

```bash
# 1. Check out / pull the branch.
# 2. Bring up the stack:
make up                # or make profile-stack-up for pg_stat_statements + ptrace

# 3. Run exactly the same command:
PERF_CONCURRENCY=10 PERF_PROBE_ENABLED=1 make profile

# 4. Compare run-metadata.json across the two runs:
diff docs/perf/2026-05-18-artifact/run-metadata.json \
     artifacts/perf/<new-ts>/run-metadata.json
```

The 48 GB run will show higher `vram_total_mb`, potentially lower `vram_used_mb_at_start`
(more headroom for model paging), and the same git commit hash — making the comparison
self-documenting. All three LLM models (qwen3:8b + qwen3:4b + qwen3-embedding:4b) fit
simultaneously in 48 GB VRAM, eliminating cold-swap latency during Pulse/RAG captures.

## What "good" looks like

- Main bundle (`index-*.js`) target: **<= 1.0 MB raw / <= 300 kB gzip**.
  As of 2026-05-10 the bundle is 1,022 kB / 301 kB gzip after Bucket G.
- GET endpoints: p99 < 50 ms cold, < 10 ms warm.
- Pulse generate: p95 dominated by LLM calls; Stage-2 already uses bounded
  concurrency (`asyncio.Semaphore(8)`), so reductions require either a faster
  model or a smaller candidate set (top_k).
- Embed: `embed_texts` already issues a single batched POST per call. There is
  no per-text loop to tune.

If a future profile shows main bundle creeping back up, the regression was
likely a new eager import of a lazy-only vendor chunk (markdown / recharts /
cytoscape). Run

```bash
grep -oE 'from"\./vendor-[^"]*\.js"' frontend/dist/assets/index-*.js | sort -u
```

to identify which vendor chunks the entry bundle eagerly imports.

---

## Confirmatory bench — operator preconditions

`scripts/perf/vllm_confirmatory_bench.sh` is a **separate, hermetic** entrypoint
(not `make profile`). It runs the full vLLM-vs-Ollama matched-pair matrix on a
GPU box and emits one verdictable bundle. Unlike `make profile` it **hard-aborts
into the bundle** on any precondition failure (never a silent meaningless run).

**Before the full run, prove the harness in ~5–8 min (zero matrix cost):**

```bash
BENCH_SMOKE=1 bash scripts/perf/vllm_confirmatory_bench.sh
```

Smoke forces Pair A only, concurrency 1, 1-paper seed, a 0.5B model, and
`VLLM_GPU_MEMORY_UTILIZATION=0.30`. It exercises every stage/trap/assert/restore
path; the verdict is **not valid** (stamped as such) — it only proves the
harness on the real box. Only run the full matrix if smoke exits 0.

**Operator preconditions (the bench preflight asserts these and aborts loudly):**

| Requirement | Why |
|---|---|
| `docker curl python3 git awk tar` on host | preflight hard-checks |
| NVIDIA GPU, `nvidia-smi` present, VRAM ≥ `MIN_VRAM_MB` (default 40000) | full matrix needs a 48 GB box; set `MIN_VRAM_MB=0` only for smoke |
| `.env` present with `EMBEDDING_DIMENSION` matching `litellm/config.yaml` `embed` alias `dimensions:` | mismatch makes every paper fail to embed; preflight drift-guard aborts early |
| `secrets/jarvis_api_key.txt`, `docker-compose.vllm.yml`, `docker-compose.perf.yml`, `litellm/config.yaml` | required files |
| Single non-deleted user **or** `API_KEY_LOGIN_ENABLED=true` in `.env`, plus an admin (auto-provisioned if DB fresh) | Scenario C session mint → C1 |
| Disk: ≥ ~20 GB free (vLLM image ~16 GB + AWQ weights + Ollama models) | image/model pulls |

**Env knobs** (all optional): `BENCH_PAIRS` (`A B`), `BENCH_CONCURRENCY`
(`4 8`), `RAG_MAX_SECONDS` (600), `BENCH_BOOT_TIMEOUT_S` (300),
`VLLM_BOOT_TIMEOUT_S` (900), `VLLM_GPU_MEMORY_UTILIZATION` (0.90 — lower if the
recorded coexistence budget warns of OOM risk vs the pinned embedder),
`COMPOSE_PROJECT_NAME` (honored if set).

**On abort the bundle self-diagnoses** — `ABORT.txt`, per-sweep `loadgen.log`,
`loadgen-FATAL.txt`, `vllm-boot-fail.log`, `env.txt` (incl. derived compose
project + VRAM budget). Copy `artifacts/perf/vllm-confirmatory-<ts>.tar.gz`
back; the agent verdicts purely from it.
