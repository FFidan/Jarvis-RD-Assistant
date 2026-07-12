# Technical Requirements

## Runtime Environment

- **Docker Engine** 24+ with Docker Compose v2
- **RAM**: 4 GB minimum, 8 GB recommended (for local LLMs via Ollama)
- **Disk**: a cold install has a one-time peak of roughly **25–53 GB** depending on your hardware tier — see [Disk budget](#disk-budget) below. The durable footprint afterward is much smaller, plus PDFs and page snapshots that accumulate over time.
- **GPU**: NVIDIA GPU optional (faster Ollama inference). On GPU, the first paper analysis takes a few minutes; on CPU-only it can take 30 minutes or more — fully supported but slower. On macOS, Docker containers cannot use the Apple GPU — expect CPU-speed analysis; allocate ≥8 GB to Docker Desktop. GPU acceleration is enabled via the `docker-compose.gpu.yml` overlay; `setup.sh` adds it automatically when it detects the Docker nvidia runtime.
- **OS**: Linux recommended. macOS supported. Windows via WSL2.
- **Python**: 3.12+ for all services

### Disk budget

A cold install writes three things to the Docker data root: the application images, the infrastructure images (Postgres, Qdrant, Ollama, LiteLLM), and the Ollama model set for your detected hardware tier. `./setup.sh --check` prints the exact figure for your tier before you commit disk to the install.

**Application image acquisition** (measured, includes containerd's compressed+unpacked blob retention):

| Path | GB |
|------|----|
| Pull prebuilt images from GHCR (v1.1.0+) | 6 |
| Build locally, CPU variant | 9 |
| Build locally, CUDA variant | 17 |

From v1.1.0, JARVIS publishes prebuilt images to GitHub Container Registry; `setup.sh` and `update.sh` prefer the pull path when prebuilt images are available, which is smaller than a local build.

**Ollama model set by hardware tier** (what `setup.sh` pulls automatically — see [hardware-and-models.md](manual/hardware-and-models.md) for the tier table):

| Tier | Model set | Size |
|------|-----------|------|
| CPU / < 10 GB VRAM | `qwen3:4b` + `qwen3-embedding:4b` | ~5 GB |
| 10–19 GB VRAM | `qwen3:8b` + `qwen3:4b` + `qwen3-embedding:4b` | ~10 GB |
| 20–39 GB VRAM | `qwen3:14b` + `qwen3:4b` + `qwen3-embedding:4b` | ~14 GB |
| ≥ 40 GB VRAM | `qwen3:30b-a3b` + `qwen3:4b` + `qwen3-embedding:4b` | ~22 GB |

**Cold-install peak = application images + 14 GB of infrastructure images + your tier's model set.** Worst case (CUDA build, top tier): 17 + 14 + 22 ≈ **53 GB**. Smallest case (GHCR pull, bottom tier): 6 + 14 + 5 ≈ **25 GB**. `setup.sh` builds/pulls the application images first, then downloads the model set, so this is a one-time additive peak, not a steady-state requirement.

After a successful install, `docker builder prune -af` reclaims the local build cache. The durable footprint settles at the per-service image sizes (`paper_ingestion` ≈2.35 GB CPU / ≈6.31 GB CUDA; `learning_engine`, `telegram_bot`, and `dashboard` well under 1 GB combined) plus your model set and a PDF/page-snapshot library that grows with use.

## Python Dependencies

`pyproject.toml` dependency groups are the canonical source; per-service
`requirements.txt` and `constraints.txt` files are generated — do not edit by hand.

```
bash scripts/export-service-requirements.sh  # regenerate
bash scripts/check-python-deps.sh            # verify parity
```

Groups: `jarvis-common` (shared), `paper-ingestion`, `paper-ingestion-optional`,
`learning-engine`, `telegram-bot`. FastAPI pinned `>=0.136.1,<0.138.0`.

Frontend (`frontend/package.json`): React ^19, TypeScript ^5.6, Vite ^8,
TanStack Query ^5, Zustand ^5, React Router ^7, Recharts ^2.15,
Cytoscape ^3.33, Lucide React, Radix UI, Playwright (dev).

Dev deps (root): `pytest>=8.0`, `pytest-asyncio>=0.24.0`, `ruff>=0.8.0`,
`httpx`, `respx>=0.21.0`.

## Infrastructure Services

| Service | Image | Purpose |
|---------|-------|---------|
| PostgreSQL | `postgres:16.8` | Main database |
| Ollama | `ollama/ollama:0.31.2` (pin in `versions.env`) | Local LLM inference |
| Qdrant | `qdrant/qdrant:v1.13.2` | Vector store for paper embeddings |
| LiteLLM | sha256-pinned (see `versions.env`) | Unified LLM gateway |
| React dashboard | `nginx:alpine` | Web dashboard (container 3000, host 3001) |

Ollama is loopback-only by default. Security posture: [docs/SECURITY.md](SECURITY.md).

## External APIs

**Free, no key:**

| API | Rate Limit | Purpose |
|-----|-----------|---------|
| arXiv | 3 req/s | Paper search and metadata |
| Semantic Scholar | 100 req/5 min (no key) | Search, citations, references |

**Discovery & Pulse sources (graceful degradation if key absent):**

| Variable | API | Notes |
|----------|-----|-------|
| `OPENALEX_API_KEY` | OpenAlex | Optional; improves rate limits, not required |
| `PUBMED_API_KEY` | PubMed E-utilities | Optional; upgrades rate limit 3→10 req/s |

**Optional citation management:** Zotero Web API (see variables below).

Not integrated: Consensus (wrong shape for date-range polling) and Google Scholar
(no official API).

## LLM Providers (at least one required)

Default `litellm/config.yaml` enables Ollama-backed aliases only.

| Option | Configuration |
|--------|--------------|
| OpenAI | Per-user encrypted key in Settings (preferred) or `OPENAI_API_KEY` in `.env` |
| Anthropic | Per-user encrypted key in Settings (preferred) or `ANTHROPIC_API_KEY` in `.env` |
| Local Ollama | No key — included in Docker Compose |
| Any OpenAI-compatible API | Configure in `litellm/config.yaml` |

## Telegram

- Bot Token required — create via [@BotFather](https://t.me/BotFather)
- Pair each user via the web dashboard: go to **Settings → Integrations → Telegram**, copy the pairing token shown there, then send `/pair <token>` to your bot in Telegram. No Chat ID lookup is needed.
- `telegram_bot` service starts only when the `telegram` profile is enabled
- **Nudge/digest scheduling** uses a single global timezone (`user.timezone` in Settings). Per-user timezone scheduling is a tracked future enhancement; in multi-user deployments all nudges fire on the single configured timezone.

## Shared Library (`libs/jarvis_common`)

Installed into each service. Key modules:

- `auth.py` — API key verification via `X-API-Key` header
- `db_helpers.py` — `dynamic_update()`, `delete_or_404()`, `init_pg_connection()`, etc.
- `http_rate_limiter.py` — inbound HTTP rate limiting
- `source_rate_limiter.py` — outbound per-source rate limiting for external APIs
- `jobs.py` — REST/SSE bridge over procrastinate; `KIND_TO_TASK` maps kind strings to tasks

Changes require rebuilding affected Docker containers.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTO_FETCH_INTERVAL_HOURS` | `0` | Automation pipeline interval (paper_ingestion) |
| `DEV_MODE` | `false` | Bypass API key auth (dev only) |
| `JARVIS_API_KEY` | — | Inter-service auth; required in production |
| `SEMANTIC_SCHOLAR_API_KEY` | — | Optional; increases S2 rate limit |
| `OPENALEX_API_KEY` | — | Optional; improves OpenAlex rate limits, not required |
| `PUBMED_API_KEY` | — | Optional NCBI key; upgrades PubMed rate limit |
| `OPENALEX_EMAIL` | — | Optional; included in OpenAlex requests for the polite pool (blank = anonymous tier) |

Zotero integration (API key, user/library ID, library type) is configured per-user
and stored encrypted at rest via **Settings → Integrations → Zotero**, not as
environment variables.

Full `.env` reference: `.env.example`. Secrets runbook: [docs/DEPLOYMENT.md](DEPLOYMENT.md).
Observability variables (Langfuse): [docs/contracts/04-observability.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/docs/contracts/04-observability.md).

## Key Dependency Floors

| Dependency | Service | Purpose |
|------------|---------|---------|
| `lxml>=6.1.0` | paper_ingestion | PubMed XML parsing + hardened XML in `cached_transport.py` |
| `scikit-learn>=1.6.0` | paper_ingestion (optional) | Per-user logistic regression on recommendation feedback |

## Secrets & Database

Secrets use Docker Secrets. Initialise locally with `bash scripts/init-secrets.sh`
(or `scripts/jarvis-setup.sh`). Full table: [docs/DEPLOYMENT.md](DEPLOYMENT.md).

Fresh installs: `db/init.sql`. Migration history: [`db/migrations/README.md`](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/db/migrations/README.md).

## Optional Reranker

How to enable it depends on which image you installed:

- **NVIDIA (CUDA) install** — the published CUDA image already ships the dependency.
  Set `RERANKER_ENABLED=true` in `.env` and restart. Nothing to rebuild.
- **CPU install** — the dependency is deliberately excluded to keep the image lean, so
  it must be built in: set `INSTALL_OPTIONAL=true` and `RERANKER_ENABLED=true` in `.env`,
  then re-run `./setup.sh --build-local`.

Without the dependency the service falls back to RRF-only ranking and logs a warning
naming the remedy. Model:
`mixedbread-ai/mxbai-rerank-base-v2` (~280 MB, downloaded from HuggingFace on first use).
