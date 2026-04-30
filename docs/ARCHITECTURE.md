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
- `queries/` - reusable SQL fragments and predicates. `predicates.py` owns
  canonical SQL predicates such as `IS_ARCHIVED_SQL` and `IS_NOT_ARCHIVED_SQL`;
  use these instead of duplicating the archived-state logic.
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

Long-running work is tracked in the shared `jobs` table and exposed through:

- `POST /api/jobs`
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/stream`
- `POST /api/jobs/{id}/cancel`

Service-specific endpoints may enqueue jobs, but their response contracts should
stay consistent with the shared job model.

## Authentication And Ownership

Current ownership helpers in `jarvis_common.auth` are single-user stubs. They
return `None` or pass through, so single-user mode works. Multi-tenant mode is
blocked until a real user resolver replaces the stubs and all read/write paths
thread `user_id` consistently.

Do not claim multi-tenant enforcement is complete until this is implemented and
verified with live tests.

## Persistence

Fresh schema is defined in `db/init.sql`; existing installs advance through
`db/migrations/`. The migration runner applies migrations on
`paper_ingestion` startup. Fresh-install validation must replay `db/init.sql`
and migrations against live Docker Postgres when schema duplication risk is in
scope.

## Specs

Durable behavioral contracts for cross-cutting workflows live in `docs/specs/`:

- [specs/2026-04-29-paper-lifecycle-redesign.md](specs/2026-04-29-paper-lifecycle-redesign.md) —
  authoritative spec for paper lifecycle states, transitions, action contracts,
  feed information architecture, recommendation feedback loop (L1+L2+L3),
  Zotero interplay, and Telegram parity. Supersedes the legacy
  `paper-lifecycle-contract.md` and `feed-information-architecture.md`, which
  are scheduled for deletion in Phase A implementation.

When changing paper status logic, lifecycle transitions, feed filtering, or the
recommender feedback loop, verify the implementation against this spec before
shipping. The spec ships in phases (see
[docs/plans/](plans/) for the META plan and per-phase implementation plans).

## Frontend Contract Boundary

The React dashboard contains meaningful workflow logic and API assumptions.
Before changing backend response shapes, job envelopes, status fields, or error
states, inspect `frontend/src/lib/api.ts`, relevant Zustand stores, pages, and
tests. Update both sides in the same patch.
