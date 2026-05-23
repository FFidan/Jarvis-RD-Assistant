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

Root `pyproject.toml` dependency groups are the canonical source for Python
dependency constraints. The per-service `requirements.txt` files contain direct
service dependencies generated from those groups, while per-service
`constraints.txt` files contain lock-derived transitive pins for Docker's
`pip install -c constraints.txt ...` build path. `libs/jarvis_common` dependencies
live in the shared `jarvis-common` group, which is included by each service group
so Docker and host `uv` resolve the shared library through the same lock. Do not
edit generated requirements or constraints files by hand.

Use:

```
bash scripts/export-service-requirements.sh  # regenerate service requirements
bash scripts/check-python-deps.sh            # verify lock + requirements parity
```

The service groups are:

| Group | Output |
|---|---|
| `jarvis-common` | Included by all service groups; no standalone requirements file |
| `paper-ingestion` | `services/paper_ingestion/requirements.txt`, `services/paper_ingestion/constraints.txt` |
| `paper-ingestion-optional` | `services/paper_ingestion/requirements-optional.txt`, `services/paper_ingestion/constraints-optional.txt` |
| `learning-engine` | `services/learning_engine/requirements.txt`, `services/learning_engine/constraints.txt` |
| `telegram-bot` | `services/telegram_bot/requirements.txt`, `services/telegram_bot/constraints.txt` |

FastAPI is intentionally capped below `0.117.0` until the Docker runtime is
upgraded in a dedicated compatibility pass.

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
| Ollama | `ollama/ollama:0.23.1` | Local LLM inference (GPU recommended); pin lives in `versions.env` as `OLLAMA_IMAGE` |
| Ollama Bootstrap | Custom init container | One-shot model pull for Ollama (runs before ollama service starts) |
| Qdrant | `qdrant/qdrant:v1.13.2` | Vector store for paper chunk embeddings |
| LiteLLM | `docker.litellm.ai/berriai/litellm@sha256:29252f25ed1b538d44f6b76ec97412c5537a180b39ede744b9f3e86ffdd278f5` | Unified LLM gateway (pull_policy: never) |
| React dashboard | `nginx:alpine` (built from `frontend/`) | Web dashboard (container port 3000; current Compose host binding 3001) |

Image pins are operational inputs, not prose-only examples: `versions.env` is the source of truth for tested third-party images, and `docker-compose.yml` keeps the same Ollama fallback (`${OLLAMA_IMAGE:-ollama/ollama:0.23.1}`). The default Ollama host publish is loopback-only (`127.0.0.1:${OLLAMA_HOST_PORT:-11434}:11434`), so browser/LAN clients cannot call the daemon directly.

**CVE-2026-7482 posture (Ollama):** keep `OLLAMA_IMAGE` at the patched tested pin above or a newer validated pin. The loopback host bind reduces host/LAN exposure, but every container attached to the `jarvis` Docker network can still reach `http://ollama:11434`; do not attach untrusted sidecars to that network. If an operator overrides JARVIS to use a shared external Ollama daemon, that daemon must be patched and bound to loopback or an equivalently trusted private network. Reopen this posture if the pin moves below the patched line, the host bind is changed away from `127.0.0.1`, a shared-daemon override is documented without equivalent controls, untrusted Docker-network peers are introduced, or a new Ollama advisory changes the fixed-version floor.

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
| OpenAI | Preferred: encrypted per-user `llm.openai.api_key` in Settings; `.env` `OPENAI_API_KEY` is bootstrap/legacy only | GPT-4o recommended for summaries |
| Anthropic | Preferred: encrypted per-user `llm.anthropic.api_key` in Settings; `.env` `ANTHROPIC_API_KEY` is bootstrap/legacy only | Claude recommended for summaries |
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

Installed into each Docker service with that service's generated constraints.
Contains cross-cutting utilities shared by paper_ingestion, learning_engine, and telegram_bot:

- `auth.py` -- API key verification via `X-API-Key` header (`verify_api_key`)
- `db_helpers.py` -- `dynamic_update()`, `delete_or_404()`, `fmt_safe()`,
  `init_pg_connection()`, `validated_model()`
- `ratelimit.py` -- `create_limiter()` with trusted-network X-Forwarded-For handling
- `jobs.py` -- thin REST/SSE bridge over procrastinate: `get_unified()` lookup helper, `list_jobs()` UNION query, `stream_job_events()` SSE bridge. Job kinds register as procrastinate tasks in `libs/jarvis_common/jarvis_common/task_registry.py`; `KIND_TO_TASK` dict maps kind strings to task objects.

Changes to `libs/jarvis_common` require rebuilding affected Docker containers.

## Environment Variables (key additions since v1.0)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTO_FETCH_INTERVAL_HOURS` | `0` (disabled) | Automation pipeline interval in paper_ingestion |
| `DASHBOARD_PASSWORD` | `` | **Deprecated** — superseded by magic-link auth (see `JARVIS_SMTP_*` settings). Ignored in current versions; kept for backward-compat recognition only. |
| `DEV_MODE` | `false` | Bypass API key auth in services (dev only) |
| `JARVIS_API_KEY` | `` | API key for inter-service auth; required in production |
| `SEMANTIC_SCHOLAR_API_KEY` | `` | Optional; increases S2 rate limit from 100/5min to 1000/5min. Also unlocks the multi-seed recommendation endpoint used by the Phase 1 Pulse discovery pipeline. |

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

### Observability additions (Langfuse operator integration)

The following env vars control the optional Langfuse observability stack. All are consumed only
when the `observability` Docker Compose profile is active. See
[docs/contracts/04-observability.md §9](contracts/04-observability.md#9-headless-provisioning)
for the full operator runbook.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OBSERVABILITY_ENABLED` | `false` | Boot-gate for the Langfuse SDK. When `false` (or unset), the SDK is never constructed — no network socket, no background thread, no latency delta. Set to `true` by `make observability-up`. |
| `LANGFUSE_HOST` | `` | Langfuse server URL seen by JARVIS services (e.g. `http://langfuse:3000`). When empty, `@observe()` decorators are no-ops. Set to `http://langfuse:3000` by `make observability-up`. |
| `LANGFUSE_INIT_USER_EMAIL` | `operator@jarvis.local` | Email for the Langfuse operator account created on first boot. |
| `LANGFUSE_INIT_USER_PASSWORD` | *(required)* | Password for the Langfuse operator account. Compose fails fast with a clear error if unset when the `observability` profile is enabled. |

The Langfuse project keypair (`langfuse_init_pk.txt` / `langfuse_init_sk.txt`) is file-only —
it is never passed as an env var and is not listed here. See §9.1 of the contract above.

Note:
- the table above records intended configuration knobs. During stabilization,
  agents and operators should verify whether an env var is actually consumed by
  the current code path before relying on it operationally. The observability
  variables in this section are part of the §9 contract in
  docs/contracts/04-observability.md and are wired as documented.

## Database Migrations

The current migration count and range are documented in [`db/migrations/README.md`](../db/migrations/README.md). Fresh installs get all tables via `db/init.sql`.
Existing installs get migrations applied automatically on startup by the auto-migration runner in
`paper_ingestion/paper_ingestion/main.py` (`run_migrations()`), tracked in `schema_migrations` table.

**Migration 018** (2026-04-11) for the Phase 1 Discovery & Pulse subsystem added:

- Three new tables: `pulse_decks` (one row per daily deck), `pulse_cards` (papers in each deck with score metadata and LLM reasoning), and a Phase 1 feedback table (user feedback 👍/👎/💾/open/dismiss — collected silently for the Phase 2 classifier). **Phase A note (migration 049, 2026-04-29):** the Phase 1 feedback table is dropped and replaced by the broader `recommendation_feedback` table that consolidates Pulse + Inbox + Paper-Detail thumbs into a single signal store; see [docs/specs/2026-04-29-paper-lifecycle-redesign.md](archive/2026-05/specs/2026-04-29-paper-lifecycle-redesign.md) §3.3 + §7.
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

**Migrations 052–053** (B.4 cutover — 2026-05-03): Migration 052 applies the procrastinate schema and brings procrastinate online as the durable task broker. Migration 053 drops the legacy `jobs` table (`DROP TABLE jobs CASCADE`), removes the `notify_jobs_update` trigger function, and deletes the `@job_handler` decorator / `worker_loop` / `_HANDLERS` registry / `enqueue()` call site. All 19 job kinds are now procrastinate tasks. The REST API contract is unchanged for backward compatibility.

## Phase 1 Discovery & Pulse dependencies (shipped)

The following Python dependency was added for the Phase 1 subsystem (now present in `services/paper_ingestion/requirements.txt`).

| Dependency | Service | Purpose |
|------------|---------|---------|
| `lxml>=5.0.0` | paper_ingestion | PubMed E-utilities returns XML; used by the new `pubmed_source.py` plugin for parsing. |

**Phase 2 planned dependencies (not Phase 1):**

| Dependency | Service | Purpose |
|------------|---------|---------|
| `scikit-learn>=1.5.0` | paper_ingestion | Per-user logistic regression classifier trained on `recommendation_feedback` for Stage 4 scoring. |
| `networkx>=3.3` or equivalent | paper_ingestion | Citation graph algorithms (PageRank, Adamic/Adar) as scoring signals. May be avoided by implementing the algorithms directly on top of asyncpg queries. |
| `bertopic>=0.16` | paper_ingestion | Optional, for the monthly "Rising Topics" widget. Heavy dep — may be deferred further. |

Phase 1 **shipped**: `scikit-learn` is in `requirements-optional.txt` and `pulse/training.py` contains the full classifier pipeline. `networkx` and `bertopic` remain deferred to Phase 2 and are only pulled in when the features that need them are implemented.

## Secrets & Files

JARVIS uses Docker Secrets for sensitive runtime values. Each secret is stored in a plain-text file under `./secrets/` (gitignored) and mounted read-only at `/run/secrets/<name>` inside the relevant container. The `_FILE` environment variable convention signals each service to read the secret from the mounted path rather than accepting the value inline.

| Secret name | Mount path | Consuming service(s) | Env var resolved |
|-------------|-----------|----------------------|-----------------|
| `postgres_password` | `/run/secrets/postgres_password` | `postgres`, `n8n` | `POSTGRES_PASSWORD_FILE` |
| `jarvis_api_key` | `/run/secrets/jarvis_api_key` | `paper_ingestion`, `learning_engine` | `JARVIS_API_KEY_FILE` |
| `qdrant_api_key` | `/run/secrets/qdrant_api_key` | `qdrant`, `paper_ingestion`, `learning_engine` | `QDRANT_API_KEY_FILE` |
| `telegram_bot_token` | `/run/secrets/telegram_bot_token` | `telegram_bot` | `TELEGRAM_BOT_TOKEN_FILE` |
| `litellm_master_key` | `/run/secrets/litellm_master_key` | `litellm`, `paper_ingestion`, `learning_engine`, `telegram_bot` | `LITELLM_MASTER_KEY_FILE` |
| `jarvis_config_key` | `/run/secrets/jarvis_config_key` | `paper_ingestion`, `learning_engine` | `JARVIS_CONFIG_KEY_FILE` |

These secret files must exist before running `docker compose up`. The canonical repo path is `bash scripts/init-secrets.sh` (or `scripts/jarvis-setup.sh`, which calls it): it creates `secrets/`, generates missing local secrets, syncs the corresponding Docker-secret files, and leaves existing values intact. Manual file population is an advanced path for operators wiring an external secrets manager; keep the same filenames, one secret value per file, mode `0600` where possible.

Plain environment variable fallbacks (e.g., `JARVIS_API_KEY`, `QDRANT_API_KEY`) remain accepted only for local development and backward compatibility. Production and shared deployments should use the helper-managed Docker Secret files and `_FILE` variables.

## Optional Reranker

The cross-encoder reranker is an optional heavy dependency gated by two flags:

1. **Build flag**: `INSTALL_OPTIONAL=true docker compose build paper_ingestion` installs `sentence-transformers`, `optimum[onnxruntime]`, and `onnxruntime` from `services/paper_ingestion/requirements-optional.txt`.
2. **Runtime flag**: `RERANKER_ENABLED=true` in `.env` activates the reranker at startup via `_HAS_RERANKER` import guard in `paper_ingestion/ingestion/reranker.py`.

Without these flags the service starts normally and falls back to RRF-only ranking. The reranker model (`mixedbread-ai/mxbai-rerank-base-v2`) is downloaded from HuggingFace Hub on first use. Note: this model is ~280 MB (vs ~22 MB for the previous ms-marco-MiniLM-L-6-v2 default) — a first-run download spike is expected.

## Migration History (024-043)

**Migrations 024-032** (2026-04-17 to 2026-04-23) cover Round-8 through Round-14 audit remediation, Zotero integration, cloud LLM key encryption, HTTPS/TLS setup, Telegram pairing hardening, and Pulse hardening. After the 2026-05-19 W1-3 squash, the canonical baseline lives in `db/init.sql`. New migrations start at 0089 in `db/migrations/`. Pre-squash files 001-088 are absorbed into the init.sql baseline and are not present as separate files.

**Migrations 033-035** (2026-04-24): WS-1 cloud LLM key encryption (migration 033), RAG answer verification metadata (migration 034), post-R14 sprint hardening (migration 035).

**Migrations 036-039** (2026-04-24 to 2026-04-26): Sprint 2-Sprint 4 audit remediation covering Zotero credential encryption, Pulse deck upsert robustness, and miscellaneous schema fixes.

**Migration 040** (2026-04-26): `paper_notes` verified-promotion column for anti-hallucination hardening.

**Migration 041** (2026-04-27): `jobs` NOTIFY/LISTEN support for lower-latency worker dispatch.

**Migration 042** (2026-04-27): `user_ownership_columns` — adds `user_id` FK columns to core tables (papers, pulse_decks, cards, projects) for multi-tenant scaffolding; writes thread `user_id` end-to-end from Sprint 6. Enforcement remains gated on the real auth resolver (see `libs/jarvis_common/jarvis_common/auth.py`).

**Migration 043** (2026-04-27): `multiuser_unique_constraints` — adds unique constraints scoped by `user_id` to prevent cross-user collisions once enforcement is activated. The Sprint 5 `papers.is_bookmarked` column + `PATCH /api/papers/{id}/bookmark` endpoint that this migration originally constrained were both removed in Phase A migration 047 (replaced by `state` ENUM + orthogonal `papers.starred` BOOLEAN); see [docs/specs/2026-04-29-paper-lifecycle-redesign.md](archive/2026-05/specs/2026-04-29-paper-lifecycle-redesign.md) §3.

**Migrations 044-049** (2026-04-29 — Phase A lifecycle redesign): see the redesign spec and migration files. Headline changes: 047 collapses 5 lifecycle booleans + status enum into a single `state` ENUM + orthogonal `starred`; 048 adds `papers.discovery_origin`; 049 drops the Phase 1 feedback table in favor of `recommendation_feedback`.
