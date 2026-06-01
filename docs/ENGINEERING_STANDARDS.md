# Engineering Standards

This document is the durable engineering standard for JARVIS RD Assistant.

Related docs:

- [ARCHITECTURE.md](ARCHITECTURE.md) - where these standards apply across services.
- [PRD.md](PRD.md) - product requirements behind user-facing behavior.

## Python

- Python 3.12+ with type hints on public function signatures.
- Pydantic v2 for request, response, and domain models.
- Async I/O for service code (`asyncpg`, `httpx`, FastAPI dependencies).
- NumPy-style docstrings for public modules, classes, and functions.
- Avoid docstrings on trivial private helpers unless they clarify a real
  contract or side effect.
- Use `ruff` for lint and formatting. Keep line length at 100.
- No hidden global mutable state for runtime collaborators. Pass dependencies
  through FastAPI dependencies, app state, explicit context objects, or job
  context.

## TypeScript And Frontend

- React 19 + TypeScript + Vite + Shadcn/ui + TanStack Query v5.
- The frontend is not passive; it contains workflow assumptions. Backend and
  frontend contracts must be verified together.
- A failed request must render as an error/degraded state, not as an empty state.
- Disable primary CTAs until prerequisites are satisfied.
- Status indicators must preserve structured degraded states.
- User-facing changes require frontend tests and, when practical, a live smoke
  check against `http://127.0.0.1:3001`.

### Typography contract

Frontend headings follow a 4-level contract (page H1 / section marker /
card title / inline label) with a "one caption per visual block" rule.
Section markers (`MarkerCaption`) and inline small-caps labels
(`MarkerLabel`) live in `frontend/src/components/typography/`. There is no
ESLint enforcement; reviewers run the hand-checklist in the contract doc
against headline-touching diffs. Canonical source:
[`contracts/08-typography.md`](contracts/08-typography.md).

## API

- HTTP endpoints use `/api/resource` REST-style paths.
- Long-running operations should use the unified jobs API unless there is an
  explicit reason not to.
- Async work acceptance should return HTTP 202 with a stable job envelope.
- Validate payloads at the boundary. Avoid `dict[str, Any]` for public job
  payloads when a Pydantic model exists.
- Sanitize SSE errors before sending them to the frontend. Use the shared
  `routers/_sse.py` helpers (`sse_event()`, `SSE_DONE`) for all SSE responses
  in `paper_ingestion`; do not inline SSE formatting.
- Health endpoints should report dependency degradation honestly.
- FastAPI lifespan setup must use `configure_lifespan` from
  `jarvis_common.app_factory`. The equal-length contract requires every init
  hook to have a corresponding teardown entry (pad with `None` if no teardown
  is needed); mismatches raise at startup.

## Database

- Schema starts in `db/init.sql`; migrations live in `db/migrations/`.
- Use parameterized SQL (`$1`, `$2`), never string interpolation for values.
- Prefer `TIMESTAMPTZ` for timestamps.
- Use JSONB for flexible evolving values.
- Tables should include `created_at TIMESTAMPTZ DEFAULT NOW()` unless there is a
  documented exception.
- Use `ON DELETE CASCADE` when the parent owns the child.
- When writing to `user_config.value`, do not `json.dumps()` values inserted with
  `::jsonb`; asyncpg's JSONB codec handles serialization.
- Migration files must not contain bare DDL outside a transaction. Run
  `bash scripts/check-migrations-no-tx.sh` to verify before adding a migration.
- State-based predicate logic is centralised in
  `paper_ingestion/queries/predicates.py` — `VIEW_PREDICATES` (10 named
  surfaces), `RECOMMENDER_EXCLUDE_SQL`, and `PULSE_CANDIDATE_EXCLUDE_SQL`.
  Use these constants; never duplicate the SQL condition inline.

## Anti-Hallucination Invariants

LLM-generated scientific content must remain evidence-backed:

- Paper metadata comes from source APIs, never from the LLM.
- Every generated finding must carry an exact quote and page number when based
  on PDF content.
- Run quote verification before storing findings.
- Drop unverifiable findings; do not ask another LLM to repair them.
- If most findings fail verification, lower confidence; if all fail, fall back
  to the original abstract.
- Generate page snapshots for verified findings where the workflow supports it.
- KG entity relationships must only persist verified evidence quotes.
- Escape or delimit untrusted text with `jarvis_common.prompt_safety` before
  inserting it into LLM prompts.
- Prompt templates belong in version-controlled code, not external workflow
  nodes.

## Jobs

- Shared job primitives and procrastinate routing live in
  `libs/jarvis_common/jarvis_common/jobs.py` and `jobs_router.py`.
- Job ownership is defined in `JOB_HANDLER_OWNER` mapping — verifies every job kind is
  assigned to the correct service queue (paper_ingestion, learning_engine, or telegram_bot).
- Procrastinate task handlers are registered via `@app.task(queue=...)` decorators
  in each service. Tests can mock or defer tasks as needed.
- At the public enqueue boundary (`jobs_router`'s discriminated-union `JobRequest`
  model), payloads are already validated into typed models before dispatch.
  Internal handlers that receive a pre-validated payload dict may work with it
  directly; typed model parsing is encouraged but not mandatory for purely
  internal handlers that never cross a service or HTTP boundary.

## Testing

Python test shape, mock policy, the carve-out registry, and the four prohibited
anti-patterns are governed by [docs/contracts/07-testing.md](contracts/07-testing.md)
— treat that contract as the single source of truth. The mechanics below are
deliberately thin; the contract carries the load-bearing rules.

- Python tests live under `services/*/tests/` (mock-unit + boundary-adapter)
  and `services/*/tests/contract/` (contract layer requiring `JARVIS_RUN_LIVE_PG=1`).
  Shared contract tests live under `libs/jarvis_common/tests/contract/`.
- Repo-root pytest uses importlib mode and excludes `live_pg`, `integration`,
  and `slow` by default. Contract tests are collected-but-skipped without
  `JARVIS_RUN_LIVE_PG=1`.
- Docker-backed tests are required for behavior that depends on live Postgres,
  Qdrant, service networking, or container-only import/runtime behavior.
- Frontend unit tests use Vitest. Browser regression tests use Playwright lanes:
  mocked, live smoke, and mutating live flows.
- Test coverage scales with blast radius. Shared contracts need broader tests
  than local helper cleanups.
- New tests MUST conform to one of the four legitimate shapes (pure-function
  unit / contract / boundary-adapter / E2E) per the testing contract; the four
  anti-patterns documented there (handler-bypass, mock-the-mock, SQL-substring,
  deep orchestration mock) are prohibited and enforced by
  [scripts/check-test-shape.py](../scripts/check-test-shape.py) on every commit.

## Docs

- Links in documentation MUST target heading slugs (e.g. `#anti-hallucination-invariants`), never GitHub `#Lxx` line anchors — line anchors are never resolved by MkDocs and will silently 404.
- Migration counts in documentation MUST reference `db/migrations/README.md` rather than hand-stamped literals; literals drift silently as new migrations are added.
