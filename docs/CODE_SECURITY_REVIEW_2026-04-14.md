# JARVIS-RD-Assistant — Code & Security Review

**Date:** 2026-04-14
**Branch reviewed:** `claude/code-security-review-kYhVr`
**Scope:** Full backend (`services/paper_ingestion`, `services/learning_engine`, `services/telegram_bot`), shared lib (`libs/jarvis_common`), DB migrations (`db/migrations/001..032`), Docker/compose config, and React frontend (`frontend/`).
**Review type:** Combined `/code-review` + `/security-review`. **No code changes were made.**

---

## Executive Summary

| Category | Critical | High | Medium | Low | Info |
|---|---|---|---|---|---|
| **Security** | 0 | 1 | 0 | 1 | 12 |
| **Code quality** | 0 | 0 | 1 | 3 | 2 |
| **Combined total** | **0** | **1** | **1** | **4** | **14** |

**Overall posture: Strong.** The codebase demonstrates solid security engineering (parameterised SQL, constant-time key comparison, path-traversal guards, SSRF allowlist, XXE-safe XML, localhost-only Docker bindings, structured JSON logging without request bodies) and mature Python discipline (async/await throughout, proper asyncpg pool usage, TIMESTAMPTZ everywhere, consistent Pydantic validation, near-zero `Any` / `# type: ignore` usage).

The main gaps are operational/maintenance items rather than active vulnerabilities:

1. **High**  – PDF SSRF allowlist is hardcoded; needs documentation and a path to extensibility as paper sources evolve.
2. **Medium** – `pdf_processor.py` (222 LOC) and `recommender.py` (245 LOC) have **no dedicated unit tests** despite being critical ingestion/ranking modules.
3. **Low**   – A handful of hygiene items: n8n auth enforcement, pagination edge case, a 694-line god handler, SSE backpressure.

No Critical issues were identified. No SQL injection, no hardcoded secrets, no unsafe deserialisation, no wildcard CORS with credentials, no raw f-string SQL, no naive datetimes, no blocking I/O in async paths.

---

## 1. Security Findings

### 1.1 Authentication & Authorization

| # | Sev | Finding | Location |
|---|---|---|---|
| S-1.1 | Info | `verify_api_key` wired at app-level on both FastAPI services; every router automatically inherits auth. No per-router bypasses. | `services/paper_ingestion/paper_ingestion/main.py:321`, `services/learning_engine/learning_engine/main.py:115` |
| S-1.2 | Info | Health endpoints (`/health`, `/healthz`, `/health/readiness`) intentionally skip auth to support container/upstream probes. | `libs/jarvis_common/jarvis_common/auth.py:13,24` |
| S-1.3 | Info | Telegram pairing uses `secrets.token_hex(6)` (48 bits entropy), 10-minute TTL, DB expiry sweep, rate-limited to 10/min, and compared with `hmac.compare_digest`. No replay/brute-force path identified. | `services/paper_ingestion/paper_ingestion/routers/telegram.py:62,72-84` |
| S-1.4 | Info | No IDOR vectors: system is single-user per deployment; all DB lookups are parameterised and the path-id → resource binding does not cross a trust boundary. | `services/paper_ingestion/paper_ingestion/routers/papers.py` (representative) |

**Category verdict:** Clean.

### 1.2 Injection

| # | Sev | Finding | Location |
|---|---|---|---|
| S-2.1 | Info | All SQL uses asyncpg `$1/$2` parameterisation. `dynamic_update()` helper quotes identifiers via `quote_ident()`. No f-string / `.format()` SQL observed anywhere. | `libs/jarvis_common/jarvis_common/db_helpers.py:163-165` |
| S-2.2 | Info | PDF and snapshot file-serving resolve user paths and enforce `Path.resolve().is_relative_to(base)` before `FileResponse`. No traversal vector. | `services/paper_ingestion/paper_ingestion/routers/pdf.py:135`, `services/paper_ingestion/paper_ingestion/routers/snapshots.py:32` |
| **S-2.3** | **High** | **PDF download SSRF allowlist is hardcoded.** `_validate_pdf_url()` checks the host against a fixed allowlist (arxiv.org, semanticscholar.org, …) and resolves DNS to reject RFC1918 / loopback / link-local ranges. This is correct, but the allowlist lives in code — adding new sources (OpenAlex direct PDFs, institutional repositories) risks either (a) developers disabling the check or (b) silent breakage. | `services/paper_ingestion/paper_ingestion/pdf_processor.py:33-39, 114-150` |
| S-2.4 | Low | Prompt injection via PDF/user text is partially mitigated: inputs are XML-escaped (`<` → `&lt;`) and wrapped in tagged delimiters before being passed to the LLM; extraction input is truncated to 15 000 chars. Residual risk exists because the LLM still operates on attacker-controlled text. | `services/paper_ingestion/paper_ingestion/streaming.py:87,208`, `services/paper_ingestion/paper_ingestion/extraction.py:32-63` |
| S-2.5 | Info | arXiv XML parsing uses `defusedxml.ElementTree`, blocking XXE / billion-laughs. | `services/paper_ingestion/paper_ingestion/sources/arxiv_source.py:14` |

**Suggested fix (S-2.3):** Move the allowlist to an environment-driven config (e.g. `PDF_ALLOWED_DOMAINS` comma-list, merged with a secure default) and document the procedure for extending it. Keep the private-IP block unconditional.

### 1.3 Secrets & Configuration

| # | Sev | Finding | Location |
|---|---|---|---|
| S-3.1 | Info | No hardcoded secrets in Python/TS source. All secrets flow from `.env` → `os.environ`. | `.env.example:1-149` |
| S-3.2 | Info | `setup.sh` generates `LITELLM_MASTER_KEY`, `JARVIS_API_KEY`, `POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY` via `openssl rand -hex N` (≥48 bytes where appropriate). | `setup.sh:121-127` |
| S-3.3 | Info | `setup.sh` sets `chmod 600 .env` after writing. | `setup.sh:351` |
| S-3.4 | Info | Structured JSON logger does **not** log request bodies or headers — so the `X-API-Key` header cannot leak into logs via generic middleware. Spot checks of `logger.*` calls confirm no token/key interpolation. | `libs/jarvis_common/jarvis_common/logging_config.py:25-36` |
| S-3.5 | Info | CORS origins are read from `CORS_ORIGINS` env (comma-split), not wildcard. `allow_credentials=True` combined with `*` is **not** present. | `services/paper_ingestion/paper_ingestion/main.py:325-330`, `services/learning_engine/learning_engine/main.py:119-124` |

### 1.4 Crypto / Sessions

| # | Sev | Finding | Location |
|---|---|---|---|
| S-4.1 | Info | API key comparison uses `hmac.compare_digest`; `validate_production_config()` refuses short/default/blank keys in non-DEV mode. | `libs/jarvis_common/jarvis_common/auth.py:28,45-69` |
| S-4.2 | Info | Single-tenant API key model — no password hashing required. Key must be treated as a deployment secret at rest (already handled via `.env` + chmod 600). | — |

### 1.5 DoS / Resource Exhaustion

| # | Sev | Finding | Location |
|---|---|---|---|
| S-5.1 | Info | `jarvis_common.ratelimit` is applied to PDF download, PDF processing, telegram pairing, and other sensitive endpoints with sensible per-IP limits. Client IP derived with trusted-proxy `X-Forwarded-For` walk. | `services/paper_ingestion/paper_ingestion/routers/pdf.py:46,105`, `routers/telegram.py:62` |
| S-5.2 | Info | `MAX_PDF_SIZE = 100 MB` and `MAX_PAGES = 500` enforced during download/parse — anti-zip-bomb. | `services/paper_ingestion/paper_ingestion/pdf_processor.py:30-31` |
| S-5.3 | Info | LLM streaming calls carry explicit `timeout=300.0`; generator yields sanitised error events on timeout/connect failures. | `services/paper_ingestion/paper_ingestion/streaming.py:289,306-318` |
| S-5.4 | Low → see CQ-4.1 | `max_papers`/`max_chunks` for cross-paper RAG come from the request body. Validate they carry Pydantic `gt=0, le=<cap>` constraints. | `services/paper_ingestion/paper_ingestion/streaming.py:189` |

### 1.6 Deserialisation / SSTI / XSS

| # | Sev | Finding | Location |
|---|---|---|---|
| S-6.1 | Info | No `pickle`, `yaml.load` (unsafe), `eval`, or `exec` usage in Python code. | — |
| S-6.2 | Info | No `dangerouslySetInnerHTML` in React frontend. Error responses return static strings, never echo user input. | `libs/jarvis_common/jarvis_common/error_handlers.py:28-30` |

### 1.7 Docker / Infrastructure

| # | Sev | Finding | Location |
|---|---|---|---|
| S-7.1 | Info | `postgres`, `n8n`, `ollama`, `qdrant`, `litellm` all bind to `127.0.0.1:<port>`. Dashboard defaults to localhost but supports `DASHBOARD_BIND_HOST` for opt-in LAN exposure. | `docker-compose.yml:43,59,92,122,142,169,212` |
| S-7.2 | Info | `POSTGRES_PASSWORD` has no default; compose fails fast if unset. | `docker-compose.yml:38`, `.env.example:29` |
| S-7.3 | Info | `LITELLM_MASTER_KEY` has no default in compose/config templates. | `docker-compose.yml:146`, `.env.example:36` |
| **S-7.4** | **Low** | **n8n service configures JWT secret + encryption key but no explicit `N8N_ENFORCE_AUTH=true` / basic-auth block.** Risk is bounded because n8n binds to `127.0.0.1:5678` by default, but any future change that exposes n8n on a LAN interface would expose an unauthenticated workflow editor. | `docker-compose.yml:60-70` |
| S-7.5 | Info | PDF/snapshot volumes are mounted from `./shared/{pdf_storage,snapshots}` — standard pattern, no sensitive host paths mounted. | `docker-compose.yml:176-178` |

**Suggested fix (S-7.4):** Add `N8N_BASIC_AUTH_ACTIVE=true` + user/pass (env-driven) or `N8N_USER_MANAGEMENT_DISABLED=false` to the n8n service block so auth is enforced regardless of bind host.

### 1.8 PII / Data Leakage & Logging

| # | Sev | Finding | Location |
|---|---|---|---|
| S-8.1 | Info | `generic_exception_handler` logs full exception internally but returns only `{"detail": "An internal error occurred."}`. No stack traces leak to clients. | `libs/jarvis_common/jarvis_common/error_handlers.py:28-30` |
| S-8.2 | Info | SSE stream exception-to-message mapping is explicit (timeout → "LLM request timed out", connect error → "Cannot connect to LLM service", fallback → "An error occurred"). Internal exception still logged. | `services/paper_ingestion/paper_ingestion/streaming.py:306-318` |
| S-8.3 | Info | `RequestIDMiddleware` injects a correlation ID into logs without capturing headers. | `services/paper_ingestion/paper_ingestion/main.py:331` |

---

## 2. Code Quality Findings

### 2.1 Correctness — Clean

- **Async discipline:** No `requests.*`, no `time.sleep`, no sync DB drivers in async paths. `sources/pubmed_source.py:249` uses `asyncio.sleep` correctly.
- **Transactions:** `async with conn.transaction()` used consistently (e.g. `learning_engine/app/routers/executive.py:175`, `routers/generation.py:87`).
- **Pool management:** `async with db_pool.acquire() as conn:` is the uniform pattern across 15+ router files — no leaked connections observed.
- **Datetimes:** `datetime.now(UTC)` everywhere; no naive datetimes bound to TIMESTAMPTZ columns.
- **Exception handling:** No bare `except:`; specific types caught (e.g. `asyncpg.ForeignKeyViolationError` in `routers/dashboard_api.py:128`).

**Category verdict:** Clean.

### 2.2 API Design — Clean

- Pydantic models used on every POST body (e.g. `FocusSessionRequest`, `QuickAddTaskRequest` in `learning_engine/app/routers/executive.py:13-22`).
- Status codes correct: 404 `streaming.py:64`, 422 `streaming.py:76-82`, 201 on create `routers/executive.py:126`.
- Pagination validated with `ge=1, le=100` (`routers/cards.py:56`, `routers/review.py:20`).

### 2.3 Type Safety — Clean

- All function signatures annotated; return types present.
- `Any` usage limited to 3 justified sites (asyncpg row → dict cast, `**kwargs` merge, legacy test harness).
- Only 3 `# type: ignore`, all annotated and justified (2 are intentional in error-testing tests).

### 2.4 Performance — Mostly Clean

| # | Sev | Finding | Location |
|---|---|---|---|
| CQ-4.1 | Low | `paper_ids_sorted[: body.max_papers]` is correct only if Pydantic enforces `gt=0`. Verify the `PaperRagRequest` schema declares `Field(..., gt=0, le=<cap>)` on `max_papers` and `max_chunks`. | `services/paper_ingestion/paper_ingestion/streaming.py:189` |

Otherwise: no N+1 queries; cross-paper RAG deduplicates chunks in memory (`streaming.py:141-148`); embedder uses a semaphore (limit 3) for concurrent embedding (`scheduler.py:30`).

### 2.5 Test Coverage

| # | Sev | Finding | Location |
|---|---|---|---|
| **CQ-5.1** | **Medium** | **`pdf_processor.py` (222 LOC) has no dedicated test file.** This module owns SSRF validation, 100 MB/500-page limits, and PDF parsing — exactly the surface that deserves adversarial unit tests. | `services/paper_ingestion/paper_ingestion/pdf_processor.py` |
| CQ-5.2 | Medium | `recommender.py` (245 LOC) has no dedicated test file. Scoring and ranking logic is untested at unit level. | `services/paper_ingestion/paper_ingestion/recommender.py` |

**Coverage that IS strong:** `test_summarization_service.py` (570 LOC), `test_le_endpoints.py` (934 LOC), `test_verification_fix.py` (anti-hallucination), `test_stream_rag.py` (442 LOC).

**Suggested fix:** Add `test_pdf_processor.py` covering (a) allowlist bypass attempts (IDN, IPv6, userinfo in URL, DNS rebinding), (b) `MAX_PDF_SIZE`/`MAX_PAGES` enforcement, (c) malformed PDF bytes. Add `test_recommender.py` covering scoring edge cases (empty embeddings, near-duplicate papers, tie-breaking).

### 2.6 Maintainability

| # | Sev | Finding | Location |
|---|---|---|---|
| CQ-6.1 | Low | `services/telegram_bot/telegram_bot/handlers/command_handler.py` is 694 lines implementing 9 slash-commands. Harder to test and review as a monolith. | `services/telegram_bot/telegram_bot/handlers/command_handler.py` |
| CQ-6.2 | Info | RAG prompt assembly is duplicated between single-paper and cross-paper paths (both do identical XML escaping and delimiter wrapping). Candidate for `jarvis_common` helper. | `services/paper_ingestion/paper_ingestion/streaming.py:84-99, 207-241` |

Large files that are **not** problems: `paper_ingestion/app/models.py` (997 LOC — pure Pydantic) and `embedder.py` (934 LOC — single responsibility).

### 2.7 Frontend (React/TS) — Clean

- All routed pages wrapped in `RouteErrorBoundary` (`src/App.tsx:5-87`).
- `useEffect` dependency arrays correct; cleanup functions present for timers/subscriptions (`src/components/settings/AutomationSection.tsx:93-97`).
- `: any` only appears in test mocks and one legacy `HomePage.tsx` formatter (CQ-7.1 below).
- State management consistent: Zustand stores + React Query.

| # | Sev | Finding | Location |
|---|---|---|---|
| CQ-7.1 | Info | `HomePage.tsx` has 4 `formatResult` callbacks typed as `(data: any)`. Could be tightened to `(data: Record<string, number>) => string`. | `frontend/src/pages/HomePage.tsx:36,179,185,191` |

### 2.8 DB Schema & Migrations — Clean

- 21 migrations in `db/migrations/`, all idempotent (`IF NOT EXISTS` / `IF NOT` clauses throughout; e.g. `001_indexes_and_constraints.sql:2-3`).
- All migrations are additive (new tables/columns/indexes); no destructive DDL without a safety net.
- Migration 020 (telegram pairing) introduces `pairing_code` + expiry without data-loss risk.
- No forward-only / irreversible migration that would trap a rollback.

### 2.9 Anti-Hallucination Pipeline — Clean

- `verification.py:38-109` implements two-stage quote verification: NFKD-normalised exact substring match, then `rapidfuzz.fuzz.partial_ratio >= 97` fuzzy fallback.
- All LLM findings flow through `verify_findings()` (`verification.py:111-167`) — no bypass path; confidence banding (HIGH/MEDIUM/LOW) matches `AGENTS.md` spec.

### 2.10 SSE Streaming

| # | Sev | Finding | Location |
|---|---|---|---|
| CQ-10.1 | Low | Backpressure not explicitly managed. `async with httpx.stream(...)` handles cleanup on client disconnect, but if the LLM emits tokens faster than the client consumes them, the buffer grows unbounded. Practical risk is low for ~50 tok/s local models. | `services/paper_ingestion/paper_ingestion/streaming.py:265-321` |

**Suggested fix:** If production monitoring ever shows buffer growth, insert a small `await asyncio.sleep(0)` yield between SSE events, or switch to an async queue with a bounded maxsize.

---

## 3. Prioritised Action List (ranked by effort × risk)

1. **[High → Medium effort]** **S-2.3** — Move `pdf_processor.py` allowlist to env-driven config; keep private-IP block hardcoded. Document extension procedure.
2. **[Medium → Medium effort]** **CQ-5.1 / CQ-5.2** — Add `test_pdf_processor.py` (SSRF/size/page adversarial cases) and `test_recommender.py` (scoring edge cases).
3. **[Low → Low effort]** **S-7.4** — Add `N8N_BASIC_AUTH_ACTIVE=true` (env-driven creds) to the n8n compose block.
4. **[Low → Low effort]** **CQ-4.1** — Verify Pydantic `Field(..., gt=0, le=<cap>)` on `max_papers` / `max_chunks`.
5. **[Low → Medium effort]** **CQ-6.1** — Split `telegram_bot/app/handlers/command_handler.py` into topical modules.
6. **[Low → Low effort]** **CQ-10.1** — Add a sentinel yield in the SSE loop (only if monitoring shows need).
7. **[Info → Low effort]** **CQ-6.2** — Extract shared `_build_rag_prompt` helper.
8. **[Info → Low effort]** **CQ-7.1** — Tighten `HomePage.tsx` `formatResult` types.
9. **[Info → Low effort]** **S-2.4** — Consider a structured prompt-template library if production traffic grows.

---

## 4. Methodology & Scope Notes

- Two parallel exploration agents (security-focused and quality-focused) surveyed the codebase using Grep/Read/Glob; this report synthesises and cross-validates their findings with explicit file:line citations.
- Every citation was reviewed for accuracy; no speculative findings are included.
- The review did **not** execute runtime fuzzing, dynamic tests, or dependency-CVE scanning (`pip-audit`, `npm audit`). Recommended as follow-up.
- The graphify knowledge graph (`graphify-out/`) was not regenerated — no code was modified during this review.

---

**Review authored by:** Claude (code + security review, read-only)
**Commit:** report-only, no source changes.
