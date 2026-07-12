# Engineering Standards

This document is the durable engineering standard for JARVIS RD Assistant.

Related docs:

- [ARCHITECTURE.md](ARCHITECTURE.md) - where these standards apply across services.
- [PRD.md](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/docs/PRD.md) - product requirements behind user-facing behavior.

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

### Typography

Frontend headings follow a 4-level caption hierarchy (Page → Navigation → Section → Card → Inline) with a "one caption per visual block" rule: each Card, TabsContent, Sheet, or Section carries at most one caption, never repeated by an immediate parent or child. Section markers (`MarkerCaption`) and inline small-caps labels (`MarkerLabel`) live in `frontend/src/components/typography/`. There is no ESLint enforcement; reviewers hand-check the rule against headline-touching diffs.

## API

- HTTP endpoints use `/api/resource` REST-style paths.
- Long-running operations should use the unified jobs API unless there is an
  explicit reason not to.
- Async work acceptance should return HTTP 202 with a stable job envelope.
- Validate payloads at the boundary. Avoid `dict[str, Any]` for public job
  payloads when a Pydantic model exists.
- Sanitize SSE errors before sending them to the frontend. Use the shared
  `jarvis_common.sse` helpers (`sse_event()`, `SSE_DONE`) for all SSE responses;
  do not inline SSE formatting.
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
  `paper_ingestion/queries/predicates.py` — `VIEW_PREDICATES` (named view surfaces) and `EXCLUDED_STATE_SQL`.
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
anti-patterns are governed by [docs/contracts/07-testing.md](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/docs/contracts/07-testing.md)
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
  [scripts/check-test-shape.py](https://github.com/limitcycle-oss/Jarvis-RD-Assistant/blob/main/scripts/check-test-shape.py) on every commit.

## LLM Prompt Shape

Every `call_llm_structured(...)` callsite under `services/` or `libs/` (excluding tests) must satisfy one of two shapes. The convention is enforced by `scripts/check-llm-prompt-shape.py`, which runs as a pre-commit hook (see `.pre-commit-config.yaml`).

### Shape A — split-role (default)

The instruction head lives in a system-role message; `prompt=` carries only data (typically wrapped via `wrap_delimited(...)` for untrusted text):

```python
safe_question, _ = wrap_delimited("user_question", question)
return await call_llm_structured(
    openai_client,
    response_model=RootModel[list[str]],
    prompt=safe_question,
    options=ChatCompletionOptions(model="fast", system=SYSTEM),
)
```

Alternatively, pass an explicit `messages=` list with a system entry when the user message is composed of multiple parts.

### Shape B — carve-out

For callsites where the prompt is fully trusted (no untrusted text interpolated), add the literal marker `# llm-prompt-shape: SINGLE-USER` on or immediately above the `call_llm_structured(` line, and document the rationale in the enclosing function's docstring. Every Shape B callsite is enumerable via its marker — `grep -rn 'llm-prompt-shape: SINGLE-USER'` lists them all.

### Key rules

- Keep instruction text in the *system* role; put untrusted data in the *user* role.
- Wrap untrusted inputs with `wrap_delimited(tag, text)` from `jarvis_common.prompt_safety` — it escapes XML-style brackets and wraps in `<tag>…</tag>`. Pass `max_chars` to `wrap_delimited`; do not pre-truncate.
- Never interpolate untrusted text into the system-role string.
- Shape B is for rare, genuinely trusted callsites — not a shortcut for migration.

### Anti-patterns to avoid

- Interpolating untrusted data into `options.system`.
- Pre-truncating before `wrap_delimited` (pass `max_chars` instead).
- Using `# llm-prompt-shape: SINGLE-USER` without a docstring rationale.

## Docs

- Links in documentation MUST target heading slugs (e.g. `#anti-hallucination-invariants`), never GitHub `#Lxx` line anchors — line anchors are never resolved by MkDocs and will silently 404.
- Migration counts in documentation MUST reference `db/migrations/README.md` rather than hand-stamped literals; literals drift silently as new migrations are added.
