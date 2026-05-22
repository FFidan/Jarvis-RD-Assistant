# 07 — Testing Contract
**Status:** LIVING
**Date:** 2026-05-22
**Reviewers must update this contract in the same patch as any change to:**
- The public surface of [libs/jarvis_common/jarvis_common/testing.py](../../libs/jarvis_common/jarvis_common/testing.py) (canonical factories)
- The set of `pytest.mark.*` markers registered in [pyproject.toml](../../pyproject.toml)
- The carve-out registry in §5 (idiomatic-mock boundaries)
- The autouse `_default_authenticated_user` stub in [services/paper_ingestion/tests/conftest.py](../../services/paper_ingestion/tests/conftest.py)
- The pre-commit test-shape checker in [scripts/check-test-shape.py](../../scripts/check-test-shape.py)

This contract describes **what a test in this repo must look like** — for every Python test added from 2026-05-22 onward. It is the steady-state counterpart to the [2026-05-22 recomposition closeout](../audit/2026-05-22-recomposition-closeout.md), which documented why the *existing* test suite drifted away from this shape.

The contract is **machine-enforceable** where practical (pre-commit hook), **policy-enforceable** otherwise (PR review against the rules in §4).

---

## 0. What this contract covers (and what it does NOT)

**In scope.**
- Python test shape — what a unit, contract, boundary-adapter, or E2E test looks like in this repo
- Mock policy — which boundaries may be mocked, which may not
- The carve-out registry (idiomatic external boundaries)
- Anti-patterns prohibited in new test code
- The canonical test infrastructure (`jarvis_common.testing`, contract layer, autouse stubs)
- The rot-on-touch policy for legacy mock-units

**Out of scope.**
- Frontend testing (Vitest + Playwright) — see [ENGINEERING_STANDARDS.md §Testing](../ENGINEERING_STANDARDS.md#testing). A future `08-frontend-testing.md` may be added if needed.
- E2E Playwright spec authorship — see [frontend/e2e/](../../frontend/e2e/) and the `:e2e:mocked` / `:e2e:live` / `:e2e:mutating` lane conventions documented in `frontend/playwright.config.ts`
- Performance benchmarks (`scripts/perf/`) — separate program
- Migration tests (`db/migrations/`) — governed by [scripts/check-migrations-no-tx.sh](../../scripts/check-migrations-no-tx.sh)

---

## 1. The four legitimate test shapes

Every new Python test MUST be one of these four shapes. Anything else is the work of the four anti-patterns in §2 — reject in code review.

### 1.1 Pure-function unit test

**Definition.** A test of a deterministic function with NO I/O dependencies — no DB, no HTTP, no filesystem, no clock-of-the-wall.

**When to use.** Parsers, validators, formatters, math, pure transforms, pydantic-model field validators, SQL-builder string output (where the function's whole contract IS the string it returns).

**Location.** Service-local `tests/test_<module>.py` or `libs/jarvis_common/tests/test_<module>.py`.

**Fixtures.** Typically none. `pytest.parametrize` for input-output tables is encouraged.

**LOC target.** ≤30 LOC per test. Most fit in 5-15 LOC.

**Population target.** ~150-250 tests across the codebase.

**Canonical example.**

```python
# libs/jarvis_common/tests/test_url_safety.py
import pytest
from jarvis_common.url_safety import safe_url


@pytest.mark.parametrize("dangerous", [
    "javascript:alert(1)",
    "data:text/html,<script>",
    "vbscript:msgbox(1)",
])
def test_safe_url_strips_dangerous_schemes(dangerous: str) -> None:
    assert safe_url(dangerous) == ""


def test_safe_url_passes_https() -> None:
    assert safe_url("https://example.com/paper") == "https://example.com/paper"
```

No mocks, no setup, no fixtures. The test IS the spec.

### 1.2 Contract test

**Definition.** A behavioral test that exercises a public surface (REST endpoint, Telegram command, shared predicate) through the **real stack** — real `asyncpg` connection to a Docker-backed Postgres, real `httpx.AsyncClient` through `ASGITransport`, real session-cookie auth, real Pydantic serialization. The only mocked components are the carve-out boundaries listed in §5.

**When to use.** Any new public endpoint. Any new shared predicate (`assert_paper_ownership`, `current_user_id_strict`, etc). Any IDOR assertion. Any behavior the user can observe through the UI or API.

**Location.** `services/<svc>/tests/contract/test_<domain>_contract.py` or `libs/jarvis_common/tests/contract/test_<predicate>_contract.py`.

**Fixtures.** `contract_conn` (per-test txn rollback) + `contract_two_users` (two seeded users + owned resources) + `SharedConnPool` wrapping the conn + the service's app fixture (`_pi_app_with_pool`, `_le_app`, etc.) + `_configure_api_key`.

**Marker boilerplate (MANDATORY, verbatim).**

```python
pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,  # opt out of _default_authenticated_user autouse stub
    pytest.mark.asyncio(loop_scope="session"),
]
```

**LOC target.** ≤40 LOC per test body. App + client fixtures shared across the file count once, not per-test.

**Population target.** ~400-500 tests across the codebase. **This is the largest layer.**

**Canonical example.**

```python
# services/paper_ingestion/tests/contract/test_account_contract.py
async def test_a1_get_account_user_b_sees_own_profile(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A1: user B gets their own profile, not user A's data.

    # Verified: services/paper_ingestion/paper_ingestion/routers/account.py:95
    # (get_account uses current_user_id_strict)
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/account")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == contract_two_users.user_b_id
    assert body["id"] != contract_two_users.user_a_id  # IDOR negative
```

Real cookie, real route, real DB, real assertion.

### 1.3 Boundary-adapter test

**Definition.** A test that verifies **our adapter** to an external boundary behaves correctly under the responses the boundary realistically returns. The external service is replaced by a controlled mock at the boundary edge (NOT inside our orchestration).

**When to use.** When adding or changing an adapter for Ollama / Qdrant / Zotero / OpenAlex / Semantic Scholar / arXiv / LiteLLM / OpenAI / Telegram Bot API / FSRS library / anki exporter.

**Location.** Service-local `tests/test_<adapter>.py` or shared sidecar tests (e.g., `test_zotero_client.py`, `test_testing_sidecars.py`).

**Fixtures.** Boundary-specific. `respx.mock` for HTTP. `MagicMock`/`AsyncMock` for libraries we wrap (FSRS, anki). `patch.dict(task_registry._TASK_MAP, ...)` for procrastinate.

**LOC target.** ≤30 LOC per test body.

**Population target.** ~80-120 tests total across the codebase.

**Canonical example.**

```python
# services/paper_ingestion/tests/test_zotero_client.py
import respx
from httpx import Response


@respx.mock
async def test_zotero_client_retries_on_429_with_retry_after_header() -> None:
    route = respx.get("https://api.zotero.org/users/123/items").mock(
        side_effect=[
            Response(429, headers={"Retry-After": "0.1"}),
            Response(200, json=[{"key": "abc", "data": {}}]),
        ]
    )
    client = ZoteroClient(user_id=123, api_key="k")
    items = await client.fetch_items()
    assert len(items) == 1
    assert route.call_count == 2  # confirms retry happened
```

The adapter is tested. The library (`httpx`) is not. The service (`api.zotero.org`) is not. Only **our adapter's behavior** under the boundary's documented responses is asserted.

### 1.4 End-to-end test

**Definition.** A Playwright browser test that exercises a critical user journey across the full stack (browser → frontend → API → DB → response → render).

**When to use.** New top-level user workflow. Regression-critical journey.

**Location.** `frontend/e2e/<workflow>.spec.ts`.

**LOC target.** ≤80 LOC per test.

**Population target.** ~30-50 tests across the codebase.

**Note.** Vitest unit tests (`frontend/src/__tests__/`) are governed by frontend conventions (see ENGINEERING_STANDARDS.md); they're not in scope of this Python contract. The same shape principles apply — test behavior, not implementation, and respect the carve-out for backend HTTP calls.

---

## 2. The four anti-patterns (prohibited in new test code)

If a test you're about to write looks like any of these, **stop**. It belongs in shape 1.1, 1.2, 1.3, or 1.4 instead, or it doesn't belong at all.

### 2.1 Handler-bypass mock test (the dominant legacy pattern)

**What it looks like.**

```python
# ❌ DO NOT WRITE
async def test_get_paper_detail_raises_404_when_missing():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    with pytest.raises(HTTPException) as exc_info:
        await papers.list_papers.__wrapped__(  # bypasses FastAPI router
            SimpleNamespace(...),
            db_pool=pool,
        )
    assert exc_info.value.status_code == 422
```

**Why it's wrong.**
- `__wrapped__` bypasses FastAPI routing, the auth dependency chain, request validation, the rate-limiter, response serialization — i.e., every layer where real bugs hide.
- The assertion (`status_code == 422`) is identical to what a one-line contract test would prove: `assert resp.status_code == 422`. The contract test ALSO proves the auth chain works, the route is registered, validation fires.
- Refactoring `list_papers`'s internal control flow breaks this test without affecting users.

**What to write instead.** A contract test (§1.2) that calls `client.get("/api/papers/...")` and asserts on the HTTP response.

### 2.2 Mock-the-mock test (zero behavioral value)

**What it looks like.**

```python
# ❌ DO NOT WRITE
mock_conn.fetchrow.return_value = FakeRecord(key="llm.smart_model", value="mistral-nemo")
result = await get_llm_smart_model(mock_pool)
assert result == "mistral-nemo"
mock_conn.fetchrow.assert_called_once_with(
    "SELECT value FROM user_config WHERE key = $1", "llm.smart_model"
)
```

**Why it's wrong.**
- The "test" arranges `mock.fetchrow.return_value`, then asserts `result` equals that same value. The assertion is `assert "mistral-nemo" == "mistral-nemo"` with extra steps.
- The `.assert_called_once_with(...)` clause locks in implementation details (the SQL query text). Refactoring to a different SQL formulation that returns the same value still breaks the test.
- It catches nothing a contract test asserting `resp.json()["smart_model"] == "mistral-nemo"` doesn't catch.

**What to write instead.** A contract test that seeds a row in `user_config` via `contract_conn` and asserts the endpoint returns it. OR delete the test — if the helper has no public surface, its caller's contract test covers it transitively.

### 2.3 SQL-substring assertion (brittle, anti-refactor)

**What it looks like.**

```python
# ❌ DO NOT WRITE
captured_sql = []

async def _capture(sql, *args):
    captured_sql.append(sql)
    return []

conn.fetch = AsyncMock(side_effect=_capture)
await list_papers_for_user(pool, user_id=7)
assert "WHERE p.user_id" in captured_sql[0]
assert "JOIN user_library" in captured_sql[0]
```

**Why it's wrong.**
- Asserts on the IMPLEMENTATION (SQL string), not the BEHAVIOR (rows returned).
- Refactoring `WHERE p.user_id = $1` → `WHERE EXISTS (SELECT 1 FROM user_library ul WHERE ...)` breaks the test even though the behavior is identical.
- Doesn't catch the bug the comment claims to catch — a SQL that LOOKS right but binds the wrong parameter still passes this test.

**What to write instead.** A contract test that seeds rows for user A and user B in a real DB, calls the endpoint as user A, and asserts user B's rows are absent. The assertion is on the BEHAVIORAL consequence — which is what the SQL is supposed to produce.

### 2.4 Deep orchestration mock

**What it looks like.**

```python
# ❌ DO NOT WRITE
mock_embedder = AsyncMock()
mock_qdrant = AsyncMock()
mock_reranker = AsyncMock()
mock_llm = AsyncMock()

mock_embedder.embed_texts.return_value = [[0.1] * 768]
mock_qdrant.query_points.return_value.points = [FakeQdrantPoint(...)]
mock_reranker.rerank.return_value = [...]
mock_llm.call_llm_structured.return_value = AnswerModel(text="...", citations=[...])

result = await answer_question("What is X?", embedder=mock_embedder, qdrant=mock_qdrant, ...)

mock_embedder.embed_texts.assert_called_once_with("What is X?")
mock_qdrant.query_points.assert_called_once()
mock_reranker.rerank.assert_called_once()
mock_llm.call_llm_structured.assert_called_once()
```

**Why it's wrong.**
- Mocking 4 external boundaries simultaneously and asserting on their call order tests the orchestration's CONTROL FLOW, not its CORRECTNESS.
- Swapping the order of two non-dependent calls (a real refactor) breaks the test without changing the answer.
- The orchestration's REAL contract is: "given a question, return an answer with citations." That's a contract test (real DB seeded with chunks) plus four boundary-adapter tests (each mock proves the adapter works against its boundary's documented responses) — not a single mega-mock test.

**What to write instead.** ONE contract test exercising the happy path against real DB + carve-out boundary mocks at the carve-out registry layer (§5), plus separate boundary-adapter tests (§1.3) for each external service.

---

## 3. Behavioral promises per shape

### 3.1 What each shape MUST do

| Shape | MUST mock | MUST NOT mock | Asserts on |
|---|---|---|---|
| Pure unit | nothing | nothing | return value / raised exception |
| Contract | only the carve-out registry (§5) | DB, FastAPI, auth, validation, serialization | HTTP response status, body shape, DB state via `contract_conn` |
| Boundary-adapter | the external service (via `respx`, `MagicMock`, etc.) | our adapter's own code | adapter's output given controlled boundary inputs |
| E2E | nothing the user can see | the frontend, the API, the DB | rendered DOM, network requests, persisted state |

### 3.2 What each shape MUST run against

| Shape | Default `uv run pytest` | `JARVIS_RUN_LIVE_PG=1 -m contract` | Frontend test runners |
|---|---|---|---|
| Pure unit | collected + passes | collected + skipped (no contract marker) | n/a |
| Contract | collected + skipped (no LIVE_PG) | collected + passes | n/a |
| Boundary-adapter | collected + passes | collected + skipped | n/a |
| E2E | n/a | n/a | `npm run test:e2e:mocked` (or `:live`, `:mutating`) |

### 3.3 What goes wrong if you skip the carve-out

If a contract test makes a real HTTP request to `api.openai.com` or starts a real Ollama embed (forgot to mock), CI becomes flaky or expensive. The carve-out registry exists for this reason.

---

## 4. Invariants (machine-checkable rules)

These are the rules [scripts/check-test-shape.py](../../scripts/check-test-shape.py) enforces on every commit touching test files. Each invariant has an ERROR or WARN level; ERROR blocks the commit.

| ID | Invariant | Level | Rationale |
|---|---|---|---|
| TS-01 | New test files MUST NOT contain `.__wrapped__(` | ERROR | Handler-bypass anti-pattern (§2.1) |
| TS-02 | New test files MUST NOT contain SQL-substring assertions (regex: `assert .* in .*sql`, `assert .*"(SELECT\|INSERT\|UPDATE\|WHERE\|JOIN)`) | ERROR | SQL-substring anti-pattern (§2.3) |
| TS-03 | New contract test files (under `tests/contract/`) MUST declare `pytest.mark.contract` in their `pytestmark` | ERROR | Required by [pyproject.toml addopts](../../pyproject.toml) marker registration |
| TS-04 | New contract test files under `services/paper_ingestion/tests/contract/` MUST declare `pytest.mark.real_auth` in their `pytestmark` | ERROR | The autouse `_default_authenticated_user` fixture would otherwise resolve `cookie_b` as user 1 (silent IDOR-test failure) |
| TS-05 | New contract test files MUST set `loop_scope="session"` on `pytest.mark.asyncio` and on any `@pytest_asyncio.fixture` | ERROR | Fixture loop-mismatch causes "Task attached to a different loop" failures (Phase B tech debt the recomposition program cleaned up) |
| TS-06 | New contract test files MUST contain at least one `# Verified: <file>:<line>` comment per `def test_*` | WARN | Grounding rule (per [CLAUDE.md](../../CLAUDE.md)) — every cited production symbol must be Read at HEAD |
| TS-07 | Test files MUST NOT redefine inline `_make_pool` / `_mock_pool` / `_make_embedder` / `_build_request` / `FakeRecord` / `_make_telegram_update` when the canonical version exists in [libs/jarvis_common/jarvis_common/testing.py](../../libs/jarvis_common/jarvis_common/testing.py) | WARN | Factory dedup — keep [jarvis_common.testing](../../libs/jarvis_common/jarvis_common/testing.py) the single source of truth |
| TS-08 | The carve-out registry (§5) MUST NOT be deleted or weakened without a paired contract update | ERROR (enforced by review) | Carve-outs protect CI cost + reliability — deleting one without a replacement plan is a real regression |

### 4.1 Why TS-01 + TS-02 are ERRORs, not WARNs

These two anti-patterns produced ~2,000 of the current legacy mock-unit tests. Allowing more would defeat the purpose of this contract. Existing instances (legacy mock-units) are grandfathered until their file is next touched (§6.1 rot-on-touch). New instances are blocked.

### 4.2 What the check does NOT do

It does NOT detect anti-patterns 2.2 (mock-the-mock) or 2.4 (deep orchestration mock) reliably — those require semantic reasoning. They're enforced by PR review, citing this contract.

---

## 5. Carve-out registry (idiomatic external boundaries — SACROSANCT)

These boundaries MAY be mocked in test code at the carve-out edge (typically in app-fixture wiring or as `respx`/`patch.dict` setup). They MUST NOT be replaced by real network calls in CI. They MUST NOT be removed from this registry without a paired contract update.

### 5.1 Network / process boundaries

| Boundary | Mock mechanism | Test population guarded |
|---|---|---|
| Ollama HTTP (`embed_texts`, qwen3 think-block, `nomic-embed-text`) | `AsyncMock` on adapter methods; `respx.mock` for raw HTTP; superseded where possible by faux-Ollama/LiteLLM sidecar (LIVE) | ~150 legacy tests, shrinking as sidecar survivors replace them |
| Cross-encoder reranker (`rerank_chunks`, `cross-encoder/ms-marco-MiniLM-L-6-v2`) | `AsyncMock` on `EmbeddingSearchMixin.rerank_chunks` | ~80 tests |
| Qdrant client (`query_points`, `RecommendQuery`, `QdrantClient`) | `MagicMock` on `app.state.qdrant`; superseded where possible by faux-Qdrant sidecar (LIVE) | ~120 legacy tests, shrinking as sidecar survivors replace them |
| `respx.mock` / `httpx_mock` for source HTTP | respx routes | ~200 tests (Zotero, S2, OpenAlex, arXiv, PubMed) |
| `AsyncOpenAI` / Langfuse / LiteLLM (Instructor-patched OpenAI) | `MagicMock` on `app.state.openai_client` | ~80 tests |
| Telegram Bot API (`bot.send_message`, `reply_text`, `Update`) | `make_telegram_update` + `AsyncMock` | ~120 tests |

### 5.2 Library boundaries

| Boundary | Mock mechanism | Test population guarded |
|---|---|---|
| `app.state.fsrs_manager` (FSRS scheduling) | `MagicMock` with `schedule_review` returning `(state, log, due)` | ~30 tests |
| `app.state.card_generator` (LLM card generation) | `AsyncMock` | ~30 tests |
| `app.state.anki_exporter` (file generation) | `MagicMock` returning bytes blob | ~10 tests |
| `patch.dict(task_registry._TASK_MAP, ...)` (procrastinate) | `patch.dict` context manager | ~60 tests |

### 5.3 Database invariants

| Boundary | Mock mechanism | Notes |
|---|---|---|
| `services/paper_ingestion/tests/test_baseline_invariants.py` (post-W1-squash invariants) | none — runs against live Postgres | All 16 tests are LIVE; **NEVER delete** |

### 5.4 Why these are carve-outs

The decision tree per boundary:

1. **Is it deterministic in CI?** No (third-party APIs, ML models, network) → mock it.
2. **Does it cost money?** Yes (LLM tokens, cloud APIs) → mock it.
3. **Is it a library we wrap, not a service?** Yes → mock the wrapper, test our adapter.
4. **Is it stateful across tests in a way pytest can't reset?** Yes (Qdrant collections, Ollama models) → mock it.

The carve-out registry is closed under "delete only with a contract update." Adding a new external boundary to the registry is fine (contract-update commit); removing one without a paired plan (e.g., replacing with a faux-Ollama sidecar) is forbidden.

---

## 6. Cleanup decisions deferred (rot-on-touch policy)

### 6.1 Existing mock-unit population (~2,000 tests)

The codebase has ~2,000 pre-existing tests that violate §2 (mostly handler-bypass + SQL-substring). They were written before this contract existed.

**Policy:** rot-on-touch.

- DO NOT run "big-bang cleanup" passes against them. Past attempts (Phase B, W4, Phase C, recomposition E2) hit a structural ceiling at ~128 deletions per pass — the survivor-citation discipline is correct but expensive.
- DO delete legacy mock-unit tests when their file is touched for any other reason (bug fix, feature work, refactor). If a contract survivor exists, cite it in the commit message; if no survivor, write one in the same PR.
- DO NOT block PRs solely on the presence of legacy mock-units in the file being touched. The touch already creates the obligation; review can flag specific candidates.

**Population estimate over time** (rough projection):
- Today: ~3,100 default-collected tests (~2,000 mock-unit + ~1,100 legitimate)
- 6 months at typical PR churn: ~2,500 default-collected
- 12 months: ~2,000 default-collected (approaching the ~700-900 healthy target as the boundary-adapter sidecars come online)

### 6.2 Faux-Ollama / faux-Qdrant sidecars (LIVE replacement path)

The recomposition closeout (§"What WOULD actually move the needle") identified that replacing mocked Ollama / Qdrant with deterministic sidecars would unlock ~100-200 more deletions per boundary. That replacement path is now LIVE for the shared `testing_sidecars` infrastructure: new adapter/contract coverage should prefer faux-Ollama/LiteLLM and faux-Qdrant sidecars when the behavior under test is our HTTP/vector integration, while keeping the §5.1 carve-outs for legacy tests and for boundary failures the sidecars do not model yet.

### 6.3 What this contract does NOT defer

It does NOT defer the rules. As of 2026-05-22, no new PR may add a §2 anti-pattern test. The pre-commit check enforces TS-01 + TS-02 + TS-03 + TS-04 + TS-05 immediately on every commit touching test files.

---

## 7. Cross-contract references

- [docs/contracts/README.md](README.md) — contract pattern, status meanings (LIVE/GHOST/PARTIAL/DEPRECATED)
- [docs/contracts/01-settings.md](01-settings.md) — settings keys (some tested by contract tests in `test_settings_contract.py`)
- [docs/contracts/02-pulse.md](02-pulse.md) — Pulse pipeline (tested by `test_pulse_contract.py` extensions)
- [docs/contracts/03-llm.md](03-llm.md) — LLM choke point (tested via boundary-adapter tests for `call_llm_structured`)
- [docs/contracts/04-observability.md](04-observability.md) — Langfuse trace boundaries (tested via boundary-adapter tests)
- [docs/contracts/05-model-lifecycle.md](05-model-lifecycle.md) — model defaults (mock the curated catalog; contract test the resolver)
- [docs/contracts/06-hardware-aware-settings.md](06-hardware-aware-settings.md) — per-machine fit indicators (pure-unit tests for the math; contract test for the API)
- [docs/ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md) — mechanics (where tests live, deselect rules)
- [docs/AGENTS.md](../../AGENTS.md) — Stabilization Guardrails reference this contract
- [docs/CLAUDE.md](../../CLAUDE.md) — Quality Gates reference this contract
- [docs/audit/2026-05-22-recomposition-closeout.md](../audit/2026-05-22-recomposition-closeout.md) — why this contract exists (the structural ceiling proof)
- [docs/audit/2026-05-22-recomposition-evidence.md](../audit/2026-05-22-recomposition-evidence.md) — carve-out floor calculation
- [docs/audit/2026-05-21-coverage-map.md](../audit/2026-05-21-coverage-map.md) — 280 rows of endpoint × coverage (Phase A foundation)

---

## 8. Verified Identifiers

Every cited symbol has been Read at HEAD `master` after the recomposition merge (commit `77470954`).

| Citation | File:line | Behavior |
|---|---|---|
| `make_pool_and_conn` canonical factory | [libs/jarvis_common/jarvis_common/testing.py:65](../../libs/jarvis_common/jarvis_common/testing.py) | `(conn, *, fetchval_return, fetchrow_return, fetch_return, raise_on_acquire, ...)` → `(pool, conn)` tuple. Pre-Phase-B inline `_make_pool` replacement. |
| `SharedConnPool` | [libs/jarvis_common/jarvis_common/testing.py:410](../../libs/jarvis_common/jarvis_common/testing.py) | Pool-shaped wrapper exposing a single contract_conn via `acquire()` AND direct pool methods (`fetch`/`fetchrow`/`fetchval`/`execute`/`executemany`). Direct-method support added during recomposition infra cleanup (2026-05-22). |
| `contract_conn` fixture | [libs/jarvis_common/jarvis_common/testing.py:373](../../libs/jarvis_common/jarvis_common/testing.py) | Per-test asyncpg connection wrapped in a transaction that rollbacks on test exit. Requires `JARVIS_RUN_LIVE_PG=1`. |
| `contract_two_users` fixture | [libs/jarvis_common/jarvis_common/testing.py:588](../../libs/jarvis_common/jarvis_common/testing.py) | Seeds two real DB users with valid session cookies + owned resources (paper, note, deck, etc.) all within the contract_conn transaction. |
| `_default_authenticated_user` autouse stub | [services/paper_ingestion/tests/conftest.py:108](../../services/paper_ingestion/tests/conftest.py) | Returns user_id=1 globally for all PI tests UNLESS test is marked `pytest.mark.real_auth`. The marker opt-out is mandatory for any PI contract test that depends on session cookies. |
| `pytest.mark.contract` registration | [pyproject.toml](../../pyproject.toml) `[tool.pytest.ini_options].markers` | `"contract: DB-backed contract test (session container + per-test txn rollback); requires JARVIS_RUN_LIVE_PG=1"` |
| `pytest.mark.real_auth` registration | [pyproject.toml](../../pyproject.toml) | `"real_auth: opt out of the autouse user-id stub; exercise the real session resolver"` |
| `pytest.mark.live_pg` registration | [pyproject.toml](../../pyproject.toml) | `"live_pg: requires Docker-backed PostgreSQL and JARVIS_RUN_LIVE_PG=1"` |
| Default `addopts` excludes | [pyproject.toml](../../pyproject.toml) | `addopts = "--import-mode=importlib -m 'not live_pg and not integration and not slow'"` — `contract` tests are collected-but-skipped without `JARVIS_RUN_LIVE_PG=1`. |
| `test_baseline_invariants.py` (DO NOT DELETE) | [services/paper_ingestion/tests/test_baseline_invariants.py](../../services/paper_ingestion/tests/test_baseline_invariants.py) | 16 post-W1-squash invariants gating the consolidated schema. Marked `live_pg`. |
| Recomposition closeout (root-cause evidence) | [docs/audit/2026-05-22-recomposition-closeout.md](../audit/2026-05-22-recomposition-closeout.md) | Plan-vs-actual + structural ceiling analysis. |
| `scripts/check-test-shape.py` (enforcement) | [scripts/check-test-shape.py](../../scripts/check-test-shape.py) | Pre-commit hook implementing TS-01..TS-08 invariants. |

---

**Updates to this contract must be paired with the code change that motivated them.** A contract that goes stale is worse than no contract.
