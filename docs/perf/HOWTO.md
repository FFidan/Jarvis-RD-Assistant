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

The profiling override lives in `docker-compose.perf.yml` and is intentionally
not loaded by normal `make up`, `make up-build`, or production compose usage.
It changes local developer observability only; it must not become part of the
default runtime security posture.

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
