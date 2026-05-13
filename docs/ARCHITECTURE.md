# Architecture

JARVIS RD Assistant is a self-hosted research assistant for paper discovery,
PDF-backed summarization, RAG, spaced repetition, project management, and
Telegram delivery.

Related docs:

- [../AGENTS.md](../AGENTS.md) - harness boot order and stabilization guardrails.
- [ENGINEERING_STANDARDS.md](ENGINEERING_STANDARDS.md) - coding, API, DB, anti-hallucination, and testing
  standards.
- [AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md) - agent evidence rules and Desloppify workflow.
- [PRD.md](PRD.md) - product requirements and durable Pulse design.
- [known-residual-risks.md](known-residual-risks.md) - accepted risks and reopen criteria.

## Runtime Topology

- `paper_ingestion` - FastAPI service for paper search, PDF processing,
  chunking, embeddings, RAG, extraction, Pulse, Zotero, and source integrations.
- `learning_engine` - FastAPI service for FSRS cards, reviews, projects,
  analytics, and card generation.
- `telegram_bot` - optional push-notification and inline-review service.
- `frontend` - React dashboard served through nginx. Host port defaults to
  `3001`; container port is `3000`.
- `postgres` - primary state store.
- `qdrant` - vector store for semantic search.
- `litellm` and `ollama` - LLM gateway and local model runtime.
- `n8n` - optional integration profile. Core scheduling uses APScheduler inside
  Python services.

Services communicate over the Docker `jarvis` network. The frontend proxies API
requests through nginx to `paper_ingestion` and `learning_engine`.

## Backend Packages

`services/paper_ingestion/paper_ingestion/` is split by responsibility:

- `routers/` - HTTP adapters. Keep business logic out of routers. SSE
  formatting uses the shared `routers/_sse.py` helpers (`sse_event()`,
  `SSE_DONE`); do not inline SSE formatting in router handlers.
- `queries/` - reusable SQL fragments and predicates. predicates.py owns
  canonical SQL predicates: VIEW_PREDICATES (10 named surfaces per spec §6), RECOMMENDER_EXCLUDE_SQL, and PULSE_CANDIDATE_EXCLUDE_SQL. Use these constants; never duplicate the SQL condition inline.
- `models/` - Pydantic models, split by domain.
- `ingestion/` - embedding, retrieval, reranking, recommendations, PDF
  processing ownership where applicable.
- `extraction/` - anti-hallucination extraction and quote verification.
- `pulse/` - proactive discovery and morning deck logic.
- `sources/` - external paper source plugins registered via the source registry.
- `integrations/` - Zotero and external service integrations.
- `services/` - internal workflow services such as summarization and local PDF
  handling.

`services/learning_engine/learning_engine/` owns FSRS scheduling, card storage,
card generation, review endpoints, projects, and analytics.

`libs/jarvis_common/` owns shared auth, database helpers (`db_helpers.py`),
prompt safety (`prompt_safety.py`), secret resolution (`crypto.py`), rate
limiting, audit/error utilities, the unified jobs primitives, and
`app_factory.py` (`configure_lifespan`) — the shared FastAPI lifespan builder
that enforces an equal-length init/teardown hook contract across services.

## Pulse

Pulse is proactive overnight paper discovery. Durable product design lives in
[PRD.md](PRD.md) sections 3.1.1 and 8.5.

Rules:

- Pulse business logic belongs in `paper_ingestion/pulse/`.
- Pulse routers stay thin.
- Source plugins implement the `PaperSource` interface and register through the
  registry.
- Scoring uses source candidates, topic/library similarity, LLM relevance and
  novelty, recency, and author signals.
- Pulse and Weekly Summary must not overlap: Pulse suggests what to read; Weekly
  Summary reflects on papers the user engaged with.

## Unified Jobs

Long-running work is brokered through **procrastinate** (PostgreSQL-backed) and
exposed through a unified HTTP envelope:

- `POST /api/jobs`
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/stream`
- `POST /api/jobs/{id}/cancel`

### Dispatch path

`POST /api/jobs` receives a `kind` string and routes it through
`KIND_TO_TASK` in `libs/jarvis_common/jarvis_common/task_registry.py`.
`KIND_TO_TASK` maps every JARVIS job kind to the registered procrastinate
task object, which `defer_async` enqueues into `procrastinate_jobs`.

### Read / stream path

`GET /api/jobs/{id}` and `GET /api/jobs/{id}/stream` use
`get_unified` and `procrastinate_row_to_jarvis_row` (both in
`libs/jarvis_common/jarvis_common/jobs.py`) to adapt a
`procrastinate_jobs` row into the public job envelope the frontend
expects.  SSE progress events are written to the sidecar
`job_progress` table (added in migration 054) and pushed to listeners
via `pg_notify`.

### Connector wiring

`task_registry.app` is a module-level `procrastinate.App` initialised
with an unconnected `AiopgConnector`.  Each service's lifespan startup
must call `task_registry.set_dependencies(pool, http_client)` before
starting the worker so every task dispatcher can access the pool and
HTTP client.

## Authentication And Ownership

Phase 1+2 (shipped 2026-05-10) replaced the single-user stubs with a real
multi-user auth system. The current state:

- **Magic-link auth** — `db/migrations/069_auth.sql` introduced `users`,
  `magic_link_tokens`, and `user_sessions` tables. `jarvis_common.auth` resolves
  the caller user from a session cookie; admin endpoints require `role='admin'`.
- **Per-user ownership** — migrations 062–070 added `user_id` columns to
  `daily_log`, `paper_recommendations`, `projects`, `tasks`, `milestones`,
  `pulse_source_health`, `system_events`, and the multi-tenant sweep table. All
  read/write paths in routers thread `user_id` from `get_current_user`.
- **IDOR guards** — router endpoints that read by PK assert ownership before
  returning data. The defensive `_resolve_request_user_id` helper (added in the
  final Phase 2 patch) tolerates mocked requests for test harnesses.
- **Per-user secrets** — Zotero, SMTP, and other per-user credentials are stored
  encrypted via `jarvis_common.crypto` (MultiFernet, `JARVIS_CONFIG_KEY`); user
  config lives in `user_config` with JSONB values.
- **Admin bootstrap** — the first-run web wizard creates the admin account; the
  admin can invite additional users via **Settings → Admin → Users**.

### Telegram Pairing

Telegram chat-to-user pairing shipped in migration 071. The dashboard issues
short-lived pairing tokens in `telegram_pairing_tokens`; the bot stores durable
chat ownership in `telegram_user_pairings`. Telegram orchestrators now iterate
paired users where the workflow has a per-user delivery surface. The legacy
`TELEGRAM_CHAT_ID` path remains a compatibility fallback for installs that have
not paired a chat yet.

### Canonical Corpus And user_library

The canonical corpus refactor shipped in migration 072:

- `papers.discovered_by` records who first introduced a canonical paper.
- `user_library(user_id, paper_id, added_via)` records personal library
  membership.
- Feed queries default to `user_library` membership for "My library" and expose
  a deliberate `scope=corpus` mode for "All discovered" while keeping
  `paper_user_state` overlays scoped to the caller.
- User-initiated uploads and manual project links add the paper to the caller's
  `user_library` in the same transaction. API-key-only single-user calls keep
  the legacy `user_id=NULL` behavior.
- In corpus scope, the Library surface maps to the `all_non_trash` predicate:
  it means "all canonical papers except trash", not caller library membership.

### Residual Risks

Known open items post-Phase-2 (see `docs/known-residual-risks.md`):

- Per-user topic subscriptions are still not implemented; auto-fetched papers
  enter only the canonical corpus until users explicitly acquire them.
- `paper_digest.py` still has a single-tenant fallback when no Telegram pairing
  exists. Remove it after production telemetry proves pairings are universal.
- IDOR regression coverage is broader than the original Phase-2 pass but is not
  yet a full live multi-user browser suite.

## Persistence

Fresh schema is defined in `db/init.sql`; existing installs advance through
`db/migrations/`. The migration runner applies migrations on
`paper_ingestion` startup. As of 2026-05-11 there are 73 migrations (001–073),
including Telegram pairings, canonical corpus, and scoped `user_config` rows.
Fresh-install validation must replay `db/init.sql` and migrations against live
Docker Postgres when schema duplication risk is in scope.

## Specs

Durable behavioral contracts for cross-cutting workflows live in `docs/specs/`:

- [archive/2026-05/specs/2026-04-29-paper-lifecycle-redesign.md](archive/2026-05/specs/2026-04-29-paper-lifecycle-redesign.md) — authoritative spec for paper lifecycle states, transitions, action contracts, feed information architecture, recommendation feedback loop (L1+L2+L3), Zotero interplay, and Telegram parity. Replaces the legacy paper-lifecycle-contract.md and feed-information-architecture.md, which were deleted as part of the Phase A atomic cutover.

When changing paper status logic, lifecycle transitions, feed filtering, or the
recommender feedback loop, verify the implementation against this spec before
shipping. The spec ships in phases (see
[docs/plans/](plans/) for the META plan and per-phase implementation plans).

## Frontend Contract Boundary

The React dashboard contains meaningful workflow logic and API assumptions.
Before changing backend response shapes, job envelopes, status fields, or error
states, inspect `frontend/src/lib/api.ts`, relevant Zustand stores, pages, and
tests. Update both sides in the same patch.
