# Architecture

JARVIS RD Assistant is a self-hosted research assistant for paper discovery,
PDF-backed summarization, RAG, spaced repetition, project management, and
Telegram delivery.

Related docs:

- [ENGINEERING_STANDARDS.md](ENGINEERING_STANDARDS.md) - coding, API, DB, anti-hallucination, and testing
  standards.
- [PRD.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/docs/PRD.md) - product requirements and durable Pulse design.
- [known-residual-risks.md](known-residual-risks.md) - accepted risks and reopen criteria.

## Runtime Topology

- `paper_ingestion` - FastAPI service for paper search, PDF processing,
  chunking, embeddings, RAG, extraction, Pulse, Zotero, and source integrations.
- `learning_engine` - FastAPI service for FSRS cards, reviews, projects,
  analytics, and card generation.
- `telegram_bot` - optional push-notification and interaction service. It is a
  thin REST client: commands, inline callbacks, and scheduled jobs (daily
  briefing, deadline warnings, author alerts) all call the `learning_engine`
  and `paper_ingestion` REST APIs using `X-Owner-User-Id` owner-override.
  Direct Postgres access is limited to the bot's own pairing table
  (`telegram_user_pairings`) and the nudge scheduler — all product-data tenant
  scoping is enforced server-side by the service endpoints the bot calls.
- `frontend` - React dashboard served through nginx. Host port defaults to
  `3001`; container port is `3000`.
- `postgres` - primary state store.
- `qdrant` - vector store for semantic search.
- `litellm` and `ollama` - LLM gateway and local model runtime.

Services communicate over the Docker `jarvis` network. The frontend proxies API
requests through nginx to `paper_ingestion` and `learning_engine`.

## Backend Packages

`services/paper_ingestion/paper_ingestion/` is split by responsibility:

- `routers/` - HTTP adapters. Keep business logic out of routers. SSE
  formatting uses the shared `jarvis_common.sse` helpers (`sse_event()`,
  `SSE_DONE`); do not inline SSE formatting in router handlers.
- `queries/` - reusable SQL fragments and predicates. predicates.py owns
  canonical SQL predicates: VIEW_PREDICATES (10 named surfaces per spec §6) and EXCLUDED_STATE_SQL (recommender + Pulse exclusion). Use these constants; never duplicate the SQL condition inline.
- `models/` - Pydantic models, split by domain.
- `ingestion/` - embedding, retrieval, reranking, recommendations, PDF
  processing ownership where applicable.
- `extraction/` - anti-hallucination extraction and quote verification.
- `pulse/` - proactive discovery and morning deck logic.
- `rag/` - retrieval-augmented generation pipeline.
- `pipelines/` - multi-step async processing pipelines.
- `jobs/` - job handler registrations and task workers.
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
[PRD.md](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/docs/PRD.md) sections 3.1.1 and 8.5.

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
`job_progress` table and pushed to listeners via `pg_notify`.

### Connector wiring

`task_registry.app` is a module-level `procrastinate.App` initialised
with an unconnected `AiopgConnector`.  Each service's lifespan startup
must call `task_registry.set_dependencies(pool, http_client)` before
starting the worker so every task dispatcher can access the pool and
HTTP client.

## Authentication And Ownership

- **Magic-link + passkey auth** — magic-link sign-in uses the `users`,
  `magic_link_tokens`, and `sessions` tables (see `db/init.sql`); optional
  WebAuthn passkeys are stored in `webauthn_credentials` (migration 0102), bound
  to the exact `APP_BASE_URL` origin with user verification required.
  `jarvis_common.auth` resolves the caller user from a session cookie; sessions
  roll forward on use (sliding 30-day expiry, throttled to one write per day);
  admin endpoints require `role='admin'`.
- **Per-user ownership** — every product row (`daily_log`,
  `paper_recommendations`, `projects`, `tasks`, `milestones`,
  `pulse_source_health`, `system_events`, and others) carries a non-NULL
  `user_id`. Single-tenant deployments are multi-tenant with exactly one user;
  there is no NULL-owned *product* data — though system-scoped configuration
  rows (e.g. `user_config` keys such as `telegram.bot_token`) are deliberately
  NULL-owned by design. The former NULL-owner backfill for most tables, and
  the follow-on backfill and per-user uniqueness constraints for
  `paper_extractions`, `paper_entities`, and Zotero `paper_notes`, are folded
  into the schema-101 baseline in `db/init.sql`. All read/write paths in
  routers thread `user_id` from `get_current_user`.
- **IDOR guards** — router endpoints that read by PK assert ownership before
  returning data. The defensive `_resolve_request_user_id` helper tolerates
  mocked requests for test harnesses.
- **Encrypted settings** — Zotero credentials are per-user. SMTP, Telegram bot,
  and cloud-provider settings are deployment-wide administrator settings. Their
  secret values are encrypted via `jarvis_common.crypto` under
  `JARVIS_CONFIG_KEY`; scope is enforced by the `user_config` key registry.
- **Admin bootstrap** — the onboarding wizard creates the admin account mid-flow
  (the admin-create step establishes the session, after which the remaining
  post-auth steps complete); the admin can invite additional users via the
  **Admin → User Management** sidebar section (`/admin/users`).

### Cross-Service Auth Boundary (resolver DI)

`jarvis_common.auth` exposes three production user-id resolvers:

- **`current_user_id_strict`** — session-only. Hard 401 without a valid session
  cookie. No `X-Owner-User-Id` path.
- **`current_user_id_with_owner_override`** — session-first, honors a verified
  `X-Owner-User-Id` header when a valid `X-API-Key` is present, but returns
  `None` (does not raise) when no identity can be resolved. Used by endpoints
  that may be reached unauthenticated.
- **`current_user_id_strict_with_owner_override`** (and its `Depends()` wrapper
  `get_current_user_id`) — the raising variant of the above: same
  `X-Owner-User-Id` override path but a hard 401 when no identity resolves.
  Required for cross-service callers (the Telegram bot) making per-user
  requests.

The override-capable resolver is applied **selectively, by reachability** — only
on endpoints that a header-authenticated caller actually reaches per-user.
Everything else uses session-only `current_user_id_strict` by design (smaller
attack surface). `scripts/check-no-unsafe-resolver.py` enforces that every
router endpoint has one of the three safe resolvers; it does not mandate the
override-capable one because the session-only choice is intentional.

### Telegram Pairing

The dashboard issues short-lived pairing tokens in `telegram_pairing_tokens`;
the bot stores durable chat ownership in `telegram_user_pairings` (see
`db/init.sql`). A chat is authenticated **exclusively** via the `/pair <token>`
flow — the token is generated in Settings → Integrations → Telegram and
submitted once. Telegram orchestrators iterate paired users where the workflow
has a per-user delivery surface. Unpaired chats receive a prompt to run
`/pair`. The legacy dashboard-code pairing path is no longer active.
The legacy `TELEGRAM_CHAT_ID` environment variable is not an authorization or
delivery fallback. Pairing records are the only user-to-chat identity source.

### Paper visibility and `user_library`

The paper schema (see `db/init.sql`) separates provenance from authorization:

- `papers.visibility_scope` is the persisted `public` or `private`
  authorization scope. Only trusted server-owned arXiv, Semantic Scholar,
  OpenAlex, and PubMed adapters may promote a row to public.
- `papers.discovered_by`, `source_type`, and `discovery_origin` are descriptive
  provenance and audit fields; they never grant access.
- `user_library(user_id, paper_id, added_via)` records personal library
  membership.
- The central policy admits a paper when it is public or is present in the
  caller's `user_library`. Feed, direct reads, graph queries, summaries,
  citations, and RAG reuse that policy.
- The Library UI calls this broader view **Public + mine**. Its internal
  `scope=corpus` request name is retained for API compatibility; it does not
  include another user's private imports.
- Local uploads, unverified citation batches, personal or group Zotero imports,
  unknown provenance, and ambiguous legacy Zotero identities stay private.
- Authenticated vector retrieval additionally requires the current visibility
  generation and a relational policy recheck, so stale or missing vector
  metadata fails closed by under-fetching.

The canonical source matrix is [Source-aware paper
visibility](SECURITY.md#source-aware-paper-visibility).

### Residual Risks

See [known-residual-risks.md](known-residual-risks.md) for the current register
of accepted deferrals and their explicit reopen criteria.

## Persistence

Fresh schema is defined in `db/init.sql`; existing installs advance through
`db/migrations/`. The migration runner applies migrations on
`paper_ingestion` startup. The current migration count and range are documented in
[`db/migrations/README.md`](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/db/migrations/README.md) — that file is the authoritative source; do not hand-stamp a literal count here.
Fresh-install validation must replay `db/init.sql` and migrations against live
Docker Postgres when schema duplication risk is in scope.

## Frontend Contract Boundary

The React dashboard contains meaningful workflow logic and API assumptions.
Before changing backend response shapes, job envelopes, status fields, or error
states, inspect `frontend/src/lib/api.ts`, relevant Zustand stores, pages, and
tests. Update both sides in the same patch.
