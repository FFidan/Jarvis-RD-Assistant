# Technical Requirements

## Runtime Environment

- **Docker Engine** 24+ with Docker Compose v2
- **RAM**: 4 GB minimum, 8 GB recommended (for local LLMs via Ollama)
- **Disk**: 20 GB+ (PDFs and page snapshots accumulate over time)
- **GPU**: NVIDIA GPU optional (faster Ollama inference). On GPU, the first paper analysis takes a few minutes; on CPU-only it can take 30 minutes or more — fully supported but slower. On macOS, Docker containers cannot use the Apple GPU — expect CPU-speed analysis; allocate ≥8 GB to Docker Desktop. GPU acceleration is enabled via the `docker-compose.gpu.yml` overlay; `setup.sh` adds it automatically when it detects the Docker nvidia runtime.
- **OS**: Linux recommended. macOS supported. Windows via WSL2.
- **Python**: 3.12+ for all services

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
| Ollama | `ollama/ollama:0.23.1` (pin in `versions.env`) | Local LLM inference |
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
Observability variables (Langfuse): [docs/contracts/04-observability.md](https://github.com/FFidan/Jarvis-RD-Assistant/blob/main/docs/contracts/04-observability.md).

## Key Dependency Floors

| Dependency | Service | Purpose |
|------------|---------|---------|
| `lxml>=6.1.0` | paper_ingestion | PubMed XML parsing + hardened XML in `cached_transport.py` |
| `scikit-learn>=1.6.0` | paper_ingestion (optional) | Per-user logistic regression on recommendation feedback |

## Secrets & Database

Secrets use Docker Secrets. Initialise locally with `bash scripts/init-secrets.sh`
(or `scripts/jarvis-setup.sh`). Full table: [docs/DEPLOYMENT.md](DEPLOYMENT.md).

Fresh installs: `db/init.sql`. Migration history: [`db/migrations/README.md`](https://github.com/FFidan/Jarvis-RD-Assistant/blob/main/db/migrations/README.md).

## Optional Reranker

Two flags required:

1. **Build**: `INSTALL_OPTIONAL=true docker compose build paper_ingestion`
2. **Runtime**: `RERANKER_ENABLED=true` in `.env`

Without these flags, the service falls back to RRF-only ranking. Model:
`mixedbread-ai/mxbai-rerank-base-v2` (~280 MB, downloaded from HuggingFace on first use).
