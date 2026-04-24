# Technical Requirements

> Operational note (2026-04-14):
> this document should be read as conservative runtime truth rather than product
> aspiration. The current local Docker setup is not a perfect match for every
> claim elsewhere in the docs. In particular:
> - Dashboard is accessible at `localhost:3001`
> - Paper Ingestion API is accessible at `localhost:8010`
> - Learning Engine API is accessible at `localhost:8011`
> - the current default LiteLLM configuration is Ollama-first; cloud-only usage
>   requires configuration review
> - `telegram_bot` is not started by plain `docker compose up`; it requires the
>   `telegram` profile
> - some documented env vars, including `SEMANTIC_SCHOLAR_API_KEY`,
>   `USER_TIMEZONE`, and `OLLAMA_MODELS`, should not be assumed to be wired
>   end-to-end without code verification
> - health endpoints return **503** when any dependency is unavailable (fixed in v1.2.2); status code is now a reliable health signal

## Runtime Environment

- **Docker Engine** 24+ with Docker Compose v2
- **RAM**: 4GB minimum, 8GB recommended (for local LLMs via Ollama)
- **Disk**: 20GB+ (PDFs and page snapshots accumulate over time)
- **GPU**: NVIDIA GPU optional (for faster Ollama inference; CPU works fine)
- **OS**: Linux recommended. macOS supported. Windows via WSL2.

## Python Version

- Python 3.12+ for all services

## Per-Service Python Dependencies

### services/paper_ingestion/requirements.txt

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
httpx>=0.27.0
PyMuPDF>=1.24.0              # PDF parsing, page rendering, text extraction
pydantic>=2.0
asyncpg>=0.30.0              # Async PostgreSQL driver
qdrant-client>=1.12.0        # Vector store client
rapidfuzz>=3.0.0             # Fast fuzzy matching (quote verification; replaced fuzzywuzzy)
tiktoken>=0.7.0              # Token counting for text chunking
slowapi>=0.1.9               # Rate limiting middleware
apscheduler>=3.10.0          # Automated fetch→embed pipeline scheduling
marker-pdf>=1.0.0              # Advanced PDF parsing with OCR
sentence-transformers>=3.0.0   # Cross-encoder reranking model
```

### services/learning_engine/requirements.txt

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
httpx>=0.27.0                # HTTP client for LiteLLM calls
pydantic>=2.0
asyncpg>=0.30.0
fsrs>=4.0.0                  # Free Spaced Repetition Scheduler (py-fsrs)
slowapi>=0.1.9               # Rate limiting middleware
genanki>=0.13.0              # Anki deck export
```

### services/telegram_bot/requirements.txt

```
python-telegram-bot>=21.0    # Async Telegram bot framework
httpx>=0.27.0                # HTTP client for internal service calls
asyncpg>=0.30.0
```

### frontend/package.json (React dashboard — PRIMARY)

```
react ^19.0               # UI framework
typescript ^5.6            # Type safety
vite ^6.0                  # Build tool + dev server
@tanstack/react-query ^5.0 # Server state management
zustand ^5.0               # Client state management
react-router-dom ^7.0      # Client-side routing
recharts ^2.15             # Charts (analytics page)
cytoscape ^3.33            # Graph visualization (KG, citation graph)
lucide-react ^0.468        # Icons
@radix-ui/* (various)      # Shadcn/ui component primitives
@playwright/test (dev)     # E2E testing framework
```

## Dev Dependencies (root level)

```
pytest>=8.0
pytest-asyncio>=0.24.0
ruff>=0.8.0                  # Linter + formatter
httpx                        # For FastAPI TestClient
respx>=0.21.0                # Mock httpx for async tests
```

## Infrastructure Services (via Docker images)

| Service | Image | Purpose |
|---------|-------|---------|
| PostgreSQL | `postgres:16.8` | Main database (all application state) |
| n8n | `docker.n8n.io/n8nio/n8n:1.77.0` | Optional workflow automation (`--profile n8n`) |
| Ollama | `ollama/ollama:0.17.7` | Local LLM inference (GPU recommended) |
| Ollama Bootstrap | Custom init container | One-shot model pull for Ollama (runs before ollama service starts) |
| Qdrant | `qdrant/qdrant:v1.13.2` | Vector store for paper chunk embeddings |
| LiteLLM | `ghcr.io/berriai/litellm:main-latest` | Unified LLM gateway (pull_policy: never) |
| React dashboard | `nginx:alpine` (built from `frontend/`) | Web dashboard (container port 3000; current Compose host binding 3001) |

## External APIs (free, no key required for basic usage)

| API | Rate Limit | Purpose |
|-----|-----------|---------|
| [arXiv API](https://arxiv.org/help/api) | 3 requests/second | Paper search and metadata |
| [Semantic Scholar API](https://api.semanticscholar.org) | 100 requests/5 minutes (no key) | Paper search, citations, references |

### Additional external APIs wired for Phase 1 Discovery & Pulse subsystem

The following APIs are integrated in the Phase 1 Discovery & Pulse subsystem (see `docs/PRD.md` §3.1.1 and §8.5). They are all free-tier and configured as optional — if the user does not provide a key, the corresponding source gracefully disables itself and Pulse runs with whatever is available.

| API | Rate Limit | Purpose | Key required? |
|-----|-----------|---------|---------------|
| [OpenAlex API](https://openalex.org) | 10k list calls/day / 1k search calls/day (free tier) | Cross-domain paper discovery — 250M+ works, author + topic filters, date-range polling | Yes (free, via `OPENALEX_API_KEY`) — mandatory since Feb 2026 |
| [PubMed E-utilities](https://www.ncbi.nlm.nih.gov/home/develop/api/) | 3 req/s (no key), 10 req/s (with key) | Biomedical paper discovery — 36M+ citations, `elink neighbor_score` for related articles | Optional (via `PUBMED_API_KEY`) — rate-limit upgrade only |
| [Unpaywall API](https://unpaywall.org) | 100k calls/day | Resolve free legal PDF URLs for paywalled papers by DOI | Yes (any email via `UNPAYWALL_EMAIL`) — required by ToS, not technical auth |

**Phase 2 additions (not Phase 1):**

| API | Purpose | Phase |
|-----|---------|-------|
| [CORE API](https://core.ac.uk) | Secondary PDF fallback source (400M+ scholarly resources) | Phase 2 |

**Phase D additions (Zotero integration):**

| API | Base URL | Required? | Purpose |
|-----|----------|-----------|---------|
| [Zotero Web API](https://www.zotero.org/support/dev/web_api/v3/start) | `https://api.zotero.org` | optional | citation management push+sync |

**Explicitly NOT integrated as Pulse sources:**

- **[Consensus](https://consensus.app)** — wrong shape for Pulse polling (it is a synthesis engine, not a date-range discovery API) and the free tier is too limited for bulk polling. Deferred to Phase 3 as a separate "Ask the Literature" feature via a documented plugin interface.
- **Google Scholar** — no official API, unofficial scrapers are fragile and against ToS. Skipped entirely.

## LLM Providers (user brings their own -- at least one required)

Current reality note:
- the repo supports multiple providers conceptually, but the checked-in default
  `litellm/config.yaml` enables Ollama-backed aliases only. OpenAI/Anthropic are
  available after configuration changes, not as the current out-of-the-box path.

| Option | Configuration | Notes |
|--------|--------------|-------|
| OpenAI | `OPENAI_API_KEY` in `.env` | GPT-4o recommended for summaries |
| Anthropic | `ANTHROPIC_API_KEY` in `.env` | Claude recommended for summaries |
| Local Ollama | No key needed | Included in Docker Compose; slower but free and private |
| Any OpenAI-compatible API | Configure in `litellm/config.yaml` | Via LiteLLM proxy routing |

## Telegram

- A Telegram Bot Token is required (create via [@BotFather](https://t.me/BotFather))
- The user's Telegram Chat ID is required (get via [@userinfobot](https://t.me/userinfobot))
- The current Compose setup starts `telegram_bot` only when the `telegram`
  profile is enabled.

## Python Environment Strategy

Per-service virtual environments for local development:

```
services/paper_ingestion/.venv/
services/learning_engine/.venv/
services/telegram_bot/.venv/
```

Managed via the root `Makefile`. In Docker, each service has its own isolated Python environment via its Dockerfile.

## Shared Libraries (local packages)

### libs/jarvis_common

Installed as editable dependency (`pip install -e libs/jarvis_common`) in each service.
Contains cross-cutting utilities shared by paper_ingestion, learning_engine, and telegram_bot:

- `auth.py` -- API key verification via `X-API-Key` header (`verify_api_key`)
- `db_helpers.py` -- `dynamic_update()`, `delete_or_404()`, `fmt_safe()`,
  `init_pg_connection()`, `validated_model()`
- `ratelimit.py` -- `create_limiter()` with trusted-network X-Forwarded-For handling
- `jobs.py` -- Unified Async Job System: `@job_handler` registry, `create_job()`,
  `update_job_status()`, `fetch_job()`, and per-service worker loop. Both
  `paper_ingestion` and `learning_engine` run their own worker loop instance, polling
  the shared `jobs` table and dispatching to registered handlers by `kind`. All
  long-running operations (pulse.generate, paper.process, paper.analyze, card.generate,
  card.generate_batch) are dispatched through this system instead of blocking HTTP
  handlers or using in-memory state.

Changes to `libs/jarvis_common` require rebuilding affected Docker containers.

## Environment Variables (key additions since v1.0)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTO_FETCH_INTERVAL_HOURS` | `0` (disabled) | Automation pipeline interval in paper_ingestion |
| `DASHBOARD_PASSWORD` | `` (no auth) | Dashboard login password; empty = open access |
| `DEV_MODE` | `false` | Bypass API key auth in services (dev only) |
| `JARVIS_API_KEY` | `` | API key for inter-service auth; required in production |
| `SEMANTIC_SCHOLAR_API_KEY` | `` | Optional; increases S2 rate limit from 100/5min to 1000/5min. Also unlocks the multi-seed recommendation endpoint used by the Phase 1 Pulse discovery pipeline. |
| `VITE_API_KEY` | `` | API key baked into React dashboard at build time |
| `VITE_DASHBOARD_PASSWORD` | `` | Dashboard login password baked into React build |

### Phase 1 Discovery & Pulse additions (shipped 2026-04-11)

The following environment variables are wired for the Phase 1 Discovery & Pulse subsystem. They are all optional — Pulse runs with whatever is provided, using graceful degradation for the rest.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENALEX_API_KEY` | `` | Free key at openalex.org; enables OpenAlex as a paper discovery source. Mandatory since Feb 2026 for OpenAlex API access. |
| `PUBMED_API_KEY` | `` | Optional free NCBI API key; upgrades PubMed rate limit from 3 to 10 requests per second. |
| `UNPAYWALL_EMAIL` | `` | Any email address; required by Unpaywall's ToS to use their free PDF resolution API. |

### Zotero integration additions (Phase D)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ZOTERO_API_KEY` | `` | Zotero Web API key (Settings → Integrations → Zotero) |
| `ZOTERO_USER_ID` | `` | Zotero user/library ID |
| `ZOTERO_LIBRARY_TYPE` | `user` | `"user"` or `"group"` |

Note:
- the table above records intended configuration knobs. During stabilization,
  agents and operators should verify whether an env var is actually consumed by
  the current code path before relying on it operationally.

## Database Migrations

32 migrations currently applied in `db/migrations/` (001-032). Fresh installs get all tables via `db/init.sql`.
Existing installs get migrations applied automatically on startup by the auto-migration runner in
`paper_ingestion/paper_ingestion/main.py` (`run_migrations()`), tracked in `schema_migrations` table.

**Migration 018** (2026-04-11) for the Phase 1 Discovery & Pulse subsystem added:

- Three new tables: `pulse_decks` (one row per daily deck), `pulse_cards` (papers in each deck with score metadata and LLM reasoning), `pulse_ratings` (user feedback 👍/👎/💾/open/dismiss — collected silently from Phase 1 for the Phase 2 classifier).
- One helper table: `pdf_resolutions` (caches results of the PDF resolution chain to dedupe resolver calls).
- One new optional column: `topics.description TEXT NULL` (free-text context for the Pulse LLM scoring prompt).
- New rows in `paper_sources` registering `openalex` and `pubmed` source types. `pubmed` ships with `enabled=TRUE` to match the "works out of the box, no key required" principle; `openalex` ships `enabled=FALSE` until the user provides a key.
- New `user_config` entries seeding Pulse settings: `pulse.enabled` (default `false`), `pulse.cron` (default `"0 4 * * *"`), `pulse.deck_size` (default `10`), `pulse.stage2_top_k` (default `50`), `pulse.weights` (JSON of scoring signal weights).

Migration 018 does not modify any existing column on any existing table — it is purely additive. See `docs/PRD.md` §3.1.1 and §8.5 for the full architectural context.

**Migrations 019-022** (2026-04-11 to 2026-04-15) are post-Phase-1 hardening fixes:
- **019**: `pdf_resolutions` UNIQUE NULLS NOT DISTINCT (dedupe resolver results correctly)
- **020**: `telegram_user_pairings` table for pairing flow (Telegram bot onboarding without requiring pre-configured TELEGRAM_CHAT_ID)
- **021**: `tracked_authors` UNIQUE NULLS NOT DISTINCT (author deduplication)
- **022**: Fix `pulse_decks.cron` JSONB double-encoding regression (Round-7 audit finding CRIT-001)

**Migration 023** (2026-04-17) for the Unified Async Job System:
- New table: `jobs` — stores all async background jobs with columns `id`, `kind`, `status` (`pending | running | done | failed | cancelled`), `payload` (JSONB input), `result` (JSONB output), `error` (TEXT), `created_at`, `updated_at`, `started_at`, `finished_at`. Indexed on `(status, created_at)` for efficient worker polling.
- New column: `paper_sources.display_order INTEGER NOT NULL DEFAULT 0` — enables drag-to-reorder of paper sources in Settings → Sources.
- New column: `pulse_decks.degraded_reason TEXT NULL` — distinguishes soft degraded runs (deck produced with fallback scoring) from fatal errors tracked in `last_error`.

## Phase 1 Discovery & Pulse dependencies (shipped)

The following Python dependency was added for the Phase 1 subsystem (now present in `services/paper_ingestion/requirements.txt`).

| Dependency | Service | Purpose |
|------------|---------|---------|
| `lxml>=5.0.0` | paper_ingestion | PubMed E-utilities returns XML; used by the new `pubmed_source.py` plugin for parsing. |

**Phase 2 planned dependencies (not Phase 1):**

| Dependency | Service | Purpose |
|------------|---------|---------|
| `scikit-learn>=1.5.0` | paper_ingestion | Per-user logistic regression classifier trained on `pulse_ratings` for Stage 4 scoring. |
| `networkx>=3.3` or equivalent | paper_ingestion | Citation graph algorithms (PageRank, Adamic/Adar) as scoring signals. May be avoided by implementing the algorithms directly on top of asyncpg queries. |
| `bertopic>=0.16` | paper_ingestion | Optional, for the monthly "Rising Topics" widget. Heavy dep — may be deferred further. |

Phase 1 explicitly ships **without** `scikit-learn`, `networkx`, and `bertopic` to keep the Phase 1 footprint minimal. Phase 2 pulls them in only when the features that need them are implemented.
