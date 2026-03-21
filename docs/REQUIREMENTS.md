# Technical Requirements

> Operational note (2026-03-10):
> this document should be read as conservative runtime truth rather than product
> aspiration. The current local Docker setup is not a perfect match for every
> claim elsewhere in the docs. In particular:
> - the default Compose host binding for the dashboard is `127.0.0.1:3001`, not `3000`
> - the default localhost bindings for the backend APIs are `127.0.0.1:8010` and `127.0.0.1:8011`, not `8000` and `8001`
> - the current default LiteLLM configuration is Ollama-first; cloud-only usage
>   requires configuration review
> - `telegram_bot` is not started by plain `docker compose up`; it requires the
>   `telegram` profile
> - some documented env vars, including `SEMANTIC_SCHOLAR_API_KEY`,
>   `USER_TIMEZONE`, and `OLLAMA_MODELS`, should not be assumed to be wired
>   end-to-end without code verification
> - health endpoints may return HTTP 200 with a JSON body whose `status` is
>   `"degraded"`, so status code alone is not a sufficient health signal

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
```

### services/learning_engine/requirements.txt

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
httpx>=0.27.0                # HTTP client for LiteLLM calls
pydantic>=2.0
asyncpg>=0.30.0
fsrs>=4.0.0                  # Free Spaced Repetition Scheduler (py-fsrs)
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
| n8n | `docker.n8n.io/n8nio/n8n:1.77.0` | Workflow orchestration |
| Ollama | `ollama/ollama:0.17.7` | Local LLM inference (GPU recommended) |
| Qdrant | `qdrant/qdrant:v1.13.2` | Vector store for paper chunk embeddings |
| LiteLLM | `ghcr.io/berriai/litellm:main-latest` | Unified LLM gateway (pull_policy: never) |
| React dashboard | `nginx:alpine` (built from `frontend/`) | Web dashboard (container port 3000; current Compose host binding 3001) |

## External APIs (free, no key required for basic usage)

| API | Rate Limit | Purpose |
|-----|-----------|---------|
| [arXiv API](https://arxiv.org/help/api) | 3 requests/second | Paper search and metadata |
| [Semantic Scholar API](https://api.semanticscholar.org) | 100 requests/5 minutes (no key) | Paper search, citations, references |

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

Changes to `libs/jarvis_common` require rebuilding affected Docker containers.

## Environment Variables (key additions since v1.0)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTO_FETCH_INTERVAL_HOURS` | `0` (disabled) | Automation pipeline interval in paper_ingestion |
| `DASHBOARD_PASSWORD` | `` (no auth) | Dashboard login password; empty = open access |
| `DEV_MODE` | `false` | Bypass API key auth in services (dev only) |
| `JARVIS_API_KEY` | `` | API key for inter-service auth; required in production |
| `SEMANTIC_SCHOLAR_API_KEY` | `` | Optional; increases S2 rate limit from 100/5min to 1000/5min |
| `VITE_API_KEY` | `` | API key baked into React dashboard at build time |
| `VITE_DASHBOARD_PASSWORD` | `` | Dashboard login password baked into React build |

Note:
- the table above records intended configuration knobs. During stabilization,
  agents and operators should verify whether an env var is actually consumed by
  the current code path before relying on it operationally.

## Database Migrations

13 migrations exist in `db/migrations/` (001-013). Fresh installs get all tables via `db/init.sql`.
Existing installs get migrations applied automatically on startup by the auto-migration runner in
`paper_ingestion/app/main.py` (`run_migrations()`), tracked in `schema_migrations` table.
