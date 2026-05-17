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

To capture span data from the four wired sites (Pulse Stage-2 LLM, embed POST, BM25 SQL,
cross-paper RAG):

```bash
# 1. Add to docker-compose.perf.yml under paper_ingestion → environment:
#      PERF_PROBE_ENABLED: "1"

# 2. Recreate the container:
make profile-stack-up   # uses docker-compose.perf.yml

# 3. Run the profile:
make profile
# → artifacts/perf/<ts>/perf-probe.jsonl will contain JSONL span records
```

The probe writes `{"name": "<span>", "elapsed_s": …, …}` JSONL lines. Each record includes
the span name and any keyword fields passed to `probe_span(name, **fields)`.

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
