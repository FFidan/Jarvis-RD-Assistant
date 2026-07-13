# Performance Profiling — HOWTO

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
docker compose exec postgres psql -U jarvis -d jarvis -c \
  "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';"

# 2. Restart Postgres:
docker compose restart postgres

# 3. Create the extension in the target DB:
docker compose exec postgres psql -U jarvis -d jarvis -c \
  "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"

# 4. Re-run the workload, then:
make profile
```

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
- `frontend/dist/bundle-stats.html` — optional Vite bundle treemap when
  `ANALYZE_BUNDLE=true`
- `backend-timings.csv` — per-endpoint wall-clock (3 runs)
- `flamegraph.svg` — py-spy 30s record (when granted ptrace)
- `pg-stat-statements-top20.csv` — when extension is preloaded
- `lighthouse.html` — when Chrome is available
- `run-metadata.json` — GPU tier, VRAM total/used at start, LiteLLM alias map,
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

**Caveats:**

- `perf_probe.py` appends JSONL synchronously inside `__exit__`, blocking the event loop. Acceptable for profiling; NOT production-safe — do not enable `PERF_PROBE_ENABLED=1` in normal deployments.
- Only `paper_ingestion` hot paths are wired; cross-service analysis requires separate instrumentation (e.g. Langfuse traces).
- **Rebuild or you get nothing.** The deployed image is a pinned tag. If it was built before the probe code landed, the JSONL is silently empty. Always rebuild from the commit under test.

**loadgen Scenario C drives the probed paths** — it mints a real owner session via `POST /api/auth/api-key-session` and exercises all four spans: `embed_texts_post`, `hybrid_search_bm25_sql`, `prepare_cross_paper_rag`, `pulse_stage2_llm`.

Preconditions:

- Needs **one non-deleted user + an admin**, or `API_KEY_LOGIN_ENABLED=true` (else Scenario C skips with exit 0).
- A **seeded corpus** makes LLM-path numbers meaningful; embed/BM25 spans fire regardless.
- `RAG_CONCURRENCY` (default ≤3) bounds `/api/ask` fan-out; `PERF_PULSE_SETTLE_SECS` (default 25) lets the async Pulse worker flush before collection.

## What "good" looks like

- Main bundle (`index-*.js`) target: **<= 1.0 MB raw / <= 300 kB gzip**.
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
