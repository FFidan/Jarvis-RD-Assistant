# Changelog

All notable changes to JARVIS RD Assistant will be documented in this file.

## [Unreleased]

### Behavior
- **Starred papers remain eligible for re-recommendation** (Sprint 7 B4). Pre-migration-044, `_filter_unread` excluded `status IN ('read','archived','starred')` from the unread candidate pool. Post-migration-044, `archived` is the explicit dismiss signal; starring is treated as a *positive* signal and no longer disqualifies a paper from future ranking. Read and archived papers continue to be filtered out.

### Added
- **Unified Async Job System** (migration 023): `jobs` table, `jarvis_common/jobs.py` module, REST endpoints (`POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/stream` SSE, `POST /api/jobs/{id}/cancel`), frontend Zustand `job-store` + TopNav `JobsIndicator` + Sonner toast notifications. Pulse generate, Paper process, Paper analyze, Generate cards (single + batch) migrated.
- **Global Pomodoro timer** in the TopBar — visible on every page when a session is active, hidden when idle. Click to navigate back to My Day.
- **Source reorder via drag-and-drop**: `paper_sources.display_order` column + `PATCH /api/sources/reorder` endpoint + `@dnd-kit/sortable` grip handle UI.
- **Discover multi-source search**: checkbox multi-select over ArXiv, Semantic Scholar, OpenAlex, PubMed with backend fan-out, dedupe, per-source error isolation.
- **Pulse Diagnostics** endpoint (`GET /api/pulse/debug`) + expandable Diagnostics section in Pulse settings tab.
- Info-bubble tooltips on source cards, notification rows, and Paper Detail action buttons.

### Changed
- **My Day redesigned** as a triage dashboard: DayHeader with counters, Pulse preview card (3 papers + link to full deck), Pomodoro + Tasks, ActionItems, Learning summary. Full Pulse deck lives at `/feed?tab=pulse`.
- **Settings** "Recommendations" tab replaced by dedicated "Pulse" tab consolidating enable/schedule/weights/generate/diagnostics. Automation tab no longer carries Pulse controls.
- Sidebar "Feed" → "Research Feed".
- `notifications.timezone` renders as "Timezone" instead of raw dotted key.
- Source cards use uniform tall layout regardless of API-key requirement.

### Fixed
- Pulse `llm_timeout` after 600s appeared as fatal error even after successful degraded runs. Now distinguishes `last_error` (fatal) vs `degraded_reason` (soft, deck produced with fallback scoring).
- "Analyze Paper" on locally-uploaded PDFs no longer errors with "no PDF URL" — local papers skip the download step.
- Generate Cards "no processed chunks" error is now a clickable link to Process PDF.
- PubMed search relevance sort no longer silently ignored (was hardcoded to `pub_date`).
- Semantic Scholar author filter now actually filters client-side (was only logging a warning).
- My Day TopBar title shows "My Day" instead of falling back to "JARVIS".

### Removed
- `learning_engine/app/jobs.py` (in-memory job state; replaced by shared jobs table).
- Legacy client-side multi-source fan-out in Discover tab (backend handles it).
- `frontend/src/components/settings/RecommendationSection.tsx` (absorbed into PulseSection).

## [1.2.6] - 2026-04-28

### Sprint 6 — Security + Reliability Hardening

Closes C1, C2, H1–H5, M1–M5, L2, L5 from the 2026-04-28 deep-audit report (`docs/plans/2026-04-28-deep-audit-security-report.md`).

#### SEC

- **C1 — `mark_paper_read` `user_id` omission**: `INSERT INTO paper_user_state (paper_id, status)` was missing the `user_id` column; every `ON CONFLICT (paper_id, user_id)` upsert resolved on `(paper_id=X, NULL)` and silently clobbered an existing `starred` row. Fixed: `user_id` now threaded and bound on every path through `mark_paper_read` and `update_paper_status`.
- **C2 — Migration 043 `user_id` constraint**: defensive PL/pgSQL constraint-name lookup added so the migration is idempotent when run on a schema that already has the constraint from a partial earlier run.
- **H1 — Telegram `paper_detail_callback` auth header**: callback was making unauthenticated GET to `paper_ingestion`; added the same `X-API-Key` header construction used by `paper_bookmark_callback`.
- **H2 — Cross-paper RAG ownership thread-through**: `prepare_cross_paper_rag` now receives and passes `user_id` into `search_chunks_global`; single-user safe (None = no filter) and ready for multi-tenant.
- **H3 — Search upsert `user_id` stamping**: `POST /api/search` → `pdf_workflow.upsert_paper` now stamps `user_id` on newly created rows.
- **H4 — `pulse_ratings` `user_id` insert**: `POST /api/pulse/rate` now includes `user_id` in the INSERT statement; rows no longer accumulate with `user_id=NULL`.
- **H5 — Migration 043 live-fixture test deferred**: constraint-name defensive PL/pgSQL ships; full live-fixture migration test deferred (see known-residual-risks.md).

#### RELIABILITY

- **M1 — `paper_user_state` write `user_id` consistency**: all four write paths (`mark_paper_read`, `bookmark_paper`, `submit_feedback`, `update_paper_status`) now thread `user_id` uniformly; ON CONFLICT targets updated to include `user_id`.
- **M2 — `paper_topics` upsert ownership**: `POST /api/search` `paper_topics` upsert now targets `ON CONFLICT (paper_id, topic_id, user_id)` instead of `(paper_id, topic_id)`.
- **M3 — Weekly summary RAG `user_id`**: `weekly_summary` passes `user_id` to `search_chunks_global` to prevent cross-user chunk leakage.
- **M4 — Bookmark mutation `onError` toast**: `PaperHeader` bookmark `useMutation` gains `onError: () => toast.error('Failed to bookmark paper')` so network failures are surfaced to the user.
- **M5 — Frozenset `extra_sets` guard**: `extra_sets` callers type-narrowed; `isinstance(s, str)` guard documented as trusted-caller assumption (see known-residual-risks.md L1).

#### LOW

- **L2 — Pyright type narrowing for `update` paths**: Optional-access errors in `update_paper_status` resolved; explicit `assert` guards added.
- **L5 — Changelog + deployment docs updated**: bookmark endpoint documented in DEPLOYMENT.md API surface table; residuals logged in known-residual-risks.md.

---

## [1.2.5] - 2026-04-27

### Sprint 5 — Multi-tenant foundations, ownership stubs, bookmark toggle, and hygiene

Closes WS-1A through WS-7 from the post-R14 roadmap sprint (`docs/superpowers/plans/2026-04-27-sprint5-closeout.md`). 7 commits on master (44f1cc6 and ancestors).

#### SEC

- **WS-3 multi-tenant ownership stubs**: migration 043 adds `user_id` columns to `papers`, `paper_user_state`, `pulse_ratings`, `paper_topics`; ownership helper stubs (`assert_paper_ownership`, `current_user_id_or_none`, `_owner_matches`) introduced in `jarvis_common/auth.py`. Single-user mode unaffected (stubs return None). `MULTITENANT_ENABLED=true` logs CRITICAL; enforcement blocked until real auth resolver ships.
- **WS-5A weight clamping**: Pulse weight sliders now clamp to [0.0, 1.0]; list param caps added to `/api/pulse/history` and `/api/feed` to prevent unbounded queries.
- **WS-5B `dynamic_update` guard + `item_key` encoding + cron validation + bookmark toggle**: `dynamic_update` rejects double-encoded JSONB; `item_key` URL-encoded before DB writes; cron expression validated via `CronTrigger.from_crontab()` before save; bookmark endpoint changed to toggle (not one-way set).

#### RELIABILITY

- **WS-4 TS build errors**: all frontend TypeScript build errors resolved; `tsc --noEmit` clean.
- **WS-4 bookmark UI wiring**: `PUT /api/papers/{id}/bookmark` client added to `lib/api.ts`; `PaperHeader` bookmark button wired to `useMutation`.

#### FEATURE

- **WS-2 RAG answer verification** (migration 034): `rag/verification.py` added; `confidence` SSE event emitted after sources; frontend badge shows verified / unverified status.
- **WS-2 snapshot thumbnails**: paper snapshot thumbnails generated on ingest; displayed in Research Feed cards.
- **WS-7 Hermes spike deferred**: build-vs-adopt decision logged in `docs/plans/2026-04-26-ws7-hermes-deferral.md`; reopen criteria defined.

#### DOCS

- **WS-6 operator docs**: `docs/known-residual-risks.md` created; Sprint 4 deferrals and Sprint 5 residuals catalogued; this CHANGELOG entry added.

---

## [1.2.4] - 2026-04-15

### Round-7 Audit Remediation

22 verified findings from the Round-7 deep audit (`docs/plans/2026-04-15-deep-audit-round7-report.md`) closed across backend, frontend, telegram, setup, and security domains.

#### CRITICAL

- **WEB-C01** — `pulse.cron` was double-JSON-encoded in `user_config`, bricking the Pulse schedule editor in the dashboard. Removed the stray `json.dumps()` in `routers/settings.py` (and a parallel instance in `main.py` for the Telegram bot username cache); added migration 022 that idempotently fixes already-double-encoded rows for `pulse.cron`, `telegram.owner_chat_id`, `pulse.weights`, `llm.smart_model`.

#### HIGH security (LAN / Tunnel mode)

- **SEC-001** — `_real_ip` in `jarvis_common.ratelimit` now walks `X-Forwarded-For` **right-to-left** (Werkzeug semantics), correctly skipping only contiguous trusted proxies at the tail. Closes the rate-limit bypass under multi-hop LAN setups.
- **SEC-002** — The `./litellm` mount in `paper_ingestion` is now read-only. `update_litellm_model` validates model names against a strict regex and re-raises `RuntimeError` on IO failure so the router returns HTTP 400 instead of a confusing stack trace.
- **SEC-006** — `CF-Connecting-IP` trust is now gated on a new env var `JARVIS_TRUST_CF_CONNECTING_IP=true` (default off). Only Tunnel-mode setups should enable it — LAN-mode users can no longer forge the header.

#### HIGH backend / reliability

- **BE-001** — `pulse.profile.load_profile` no longer holds a DB pool connection across the `embed_texts()` HTTP call (3-phase refactor: acquire → release → http → acquire).
- **BE-002** — Pulse stage-2 LLM rerank narrows exception handling to `(json.JSONDecodeError, ValueError, RuntimeError, httpx.HTTPError)` and removes the inline `import json as _json` closure.
- **BE-003** — Hybrid-search pagination now pushes `offset` into `hybrid_search` server-side; the RRF-merged ranking is sliced after fusion so relative rankings are preserved.
- **BE-004** — Deleted `services/paper_ingestion/app/pulse/resolver.py` (~230 LOC dead module) and its test file. `pdf_resolutions` table drop deferred.
- **BE-005** — Wrapped the read-then-write paths in `author_alerts`, `daily_briefing`, `project_manager.update_daily_log`, and analytics in explicit `acquire()` blocks (with `transaction()` where TOCTOU matters); single-query functions intentionally left alone.
- **BE-010** — `pulse.run_pulse` step 7 now wraps `upsert_paper` + `persist_deck` in a single transaction — no orphaned papers on mid-crash.

#### HIGH frontend / Telegram

- **FE-001** — `PulseDeck` navigates to `/paper/:id` (singular) matching the router. Clicking a Pulse card on My Day no longer 404s.
- **FE-002** — `apiFetchRaw` now translates abort errors to a friendly `ApiError` in both Anki and CSV export flows via a shared `_handleFetchError` helper. `setup.ts` gains an `AbortSignal.any` polyfill for jsdom.
- **TG-001 + TG-002 + TG-003** — `/help` lists all 14 commands (grouped by Papers / Learning / Projects & Tasks / General); `set_my_commands` runs on `post_init` so the Telegram `/` autocomplete menu works; "Start Review" inline button actually starts a review instead of printing "Use /review to start".
- **TG-011** — `telegram_bot` container now has a 10 MB × 3 log cap.

#### HIGH UX / setup

- **UX-001** — New `ollama-bootstrap` init container pulls every model in `OLLAMA_MODELS` before `paper_ingestion` starts. First RAG query on a fresh install no longer fails with "model not found".
- **UX-002** — Setup wizard Automation step co-writes `pulse.enabled=true` alongside `pulse.cron` with an explicit opt-out checkbox; first-login users get a Pulse deck overnight instead of a silent disabled scheduler.
- **UX-004** — `setup.sh` calls `wait_healthy()` (lifted from `update.sh`) before printing "Setup complete" — the banner is no longer a lie.
- **UX-005** — `.env.example` Telegram section relabeled OPTIONAL with blank values and a comment about the `--profile telegram` opt-in.

#### MED

- **BE-010** (atomic pulse persist) — see above, bundled with HIGH backend block.

### Database Migrations

- **022** — `022_fix_pulse_cron_double_encode.sql`: idempotent fix for WEB-C01 affecting 4 known `user_config` keys.

### Falsified (no fix needed)

- **BE-009** — Both `paper_ingestion` and `learning_engine` `health_check` already wrap `conn.fetchval` in `asyncio.wait_for(..., timeout=5.0)`. Report-authored claim was incorrect.
- **SEC-003 (partial)** — `setup.sh` tunnel mode does prompt for a tunnel hostname, but lacks an explicit Zero Trust Access acknowledgment gate. Handled via expanded `docs/personal/SETUP_SECOND_MACHINE.md` §7b.3 deployment-mode warning instead of a code-level gate.

## [1.2.3] - 2026-04-14

### Security Review Remediation

External code + security review delivered 10 findings. 6 were falsified on verification; 4 real items were fixed. Full report: `docs/CODE_SECURITY_REVIEW_2026-04-14.md`.

#### Prompt injection mitigation (S-2.4 MEDIUM)

- **New `jarvis_common.prompt_safety`** — `escape_llm_text()` replaces `<`/`>` with HTML entities; `wrap_delimited(tag, text)` escapes + wraps in `<tag>…</tag>` delimiters. Exported from `jarvis_common.__init__`.
- Applied to all four remaining LLM call sites that lacked input escaping: `entity_extractor.build_entity_prompt`, `services/summarization.generate_paper_summary`, `decomposition.decompose_query`, `pulse/prompts.build_scoring_prompt`.
- Replaced 4 inline `.replace('<', '&lt;')` chains in `streaming.py` with `escape_llm_text()`.
- **AGENTS.md rule #11**: all untrusted text inserted into LLM prompts must use `escape_llm_text()` or `wrap_delimited()`.
- New tests: `libs/jarvis_common/tests/test_prompt_safety.py` (16 tests), `services/paper_ingestion/tests/test_prompt_injection_mitigation.py` (10 tests).

#### SSRF test coverage (CQ-5.1 MEDIUM)

- New `services/paper_ingestion/tests/test_pdf_processor.py` — 35 adversarial unit tests for `_validate_pdf_url` and the size/page caps. Covers: 5 allowed-host pass, private/loopback/link-local/reserved/IPv6-private IP rejection, 169.254.254.254 AWS metadata endpoint, userinfo-in-URL, IDN, scheme rejection, malformed URL, DNS failure, DNS rebinding (public+private multi-result). Also `MAX_PDF_SIZE` (100 MB) and `MAX_PDF_PAGES` (500) caps.

#### Recommender scoring coverage (CQ-5.2 MEDIUM)

- Extracted `_compute_score(liked, project, liked_weight, project_weight) -> float` from `refresh_recommendations` in `recommender.py`.
- New `services/paper_ingestion/tests/test_recommender.py` — 28 tests in 4 classes covering weight formula (liked×0.6 + project×0.4), filter/dedup logic, and integration happy path.

#### `command_handler.py` domain split (CQ-6.1 LOW)

- Split 694-line monolith into `services/telegram_bot/app/handlers/commands/` package: `paper_commands.py`, `project_commands.py`, `task_commands.py`, `system_commands.py`, `_auth.py`, `registry.py`, `__init__.py`.
- `command_handler.py` reduced to DEPRECATED re-export stub for backward compatibility.

#### Falsified findings (no fix)

S-2.3 (PDF SSRF allowlist), S-7.4 (n8n basic auth), CQ-4.1 (Pydantic bounds), CQ-10.1 (SSE backpressure), CQ-7.1 (HomePage any callbacks), CQ-6.2 (RAG prompt duplication). Justifications in `docs/CODE_SECURITY_REVIEW_2026-04-14.md`.

## [1.2.2] - 2026-04-14

### Round-6 Deep Audit + Security Hardening

27-commit audit sprint addressing 1 CRITICAL, 2 CRITICAL ship-blockers, ~20 HIGH, and ~26 MEDIUM findings. Full report: `docs/plans/2026-04-13-deep-audit-round6-report.md`. Pyright error count: 90 → 0.

#### CRITICAL fixes

- **C1 — KG anti-hallucination**: `entity_extractor.extract_entities_for_paper` now routes every edge through `QuoteVerifier` strict-skip path; edges without a verified verbatim quote are dropped rather than saved. Previously, KG edges could be hallucinated.
- **C2/C3 — Pairing ship-blockers**: `BotConfig.telegram_chat_id` is now Optional; `resolve_owner_chat_id()` reads from DB if env var unset, wiring the deep-link pairing flow end-to-end. Removed duplicate port 3001 bind that prevented LAN deployments from starting.

#### HIGH security fixes

- **Pairing security (H3/H4)**: `create_pairing` wrapped in transaction; expire-only sweep prevents table wipe; rate limit 10/min; existing-owner check; pairing code hashed (SHA256) before logging.
- **XFF + rate-limit trust (H7)**: Rate limiter now walks `X-Forwarded-For` left-to-right for correct rightmost-trusted-hop extraction. `CF-Connecting-IP` preferred when present. New `TRUSTED_PROXY_CIDRS` env var.
- **API key storage (H8)**: Moved from `localStorage` to `sessionStorage` (tab-scoped). Closing the tab clears the key. Logout also clears `jarvis-ui` store.
- **SSE 401 triggers logout (H9/H10)**: Frontend `apiFetch` now combines caller `AbortSignal` with internal timeout signal; SSE 401 response triggers auth-store logout.
- **Health endpoints return 503 (H14/H15/H17)**: Both `paper_ingestion` and `learning_engine` `/health` endpoints now return 503 (not 200) when any dependency is unavailable. Docker `depends_on: service_healthy` cascades correctly.
- **DB connection leak (H12/H13)**: `pdf_processor.download_pdf()` no longer holds the asyncpg connection across the HTTP download. Per-file connection in the PDF scan loop.
- **Advisory lock bounded (H16)**: `pg_advisory_xact_lock(42)` in the migration runner now has a 60-second timeout. Previously unbounded.
- **Backup AWS guard (H18)**: `scripts/backup.sh` checks for `aws` in `$PATH` before S3 upload; fails with a clear warning instead of silently skipping.
- **Rate limits extended (H11/M5/M3)**: Added to recommendations, setup-status, and all Telegram bot commands.

#### Other fixes

- **nginx CSP (M32/M33)**: Security headers extracted to nginx snippet; Content-Security-Policy additions.
- **Setup/HTTPS hardening**: LAN port dedup via `DASHBOARD_BIND_HOST`, SAN cert propagation, tunnel CORS hostname propagation.
- **Pulse MEDIUM (W4.7)**: Real LLM call counter, PubMed sort order, deck batch fetch, migration 021 (`tracked_authors` NULLS NOT DISTINCT + tombstone in `resolver.py`).
- **Cards async (M19)**: `batch_generate_cards` returns 202 + background task instead of blocking.
- **Pyright 90 → 0**: 71 Optional-access errors in telegram_bot handlers resolved; learning_engine type-safety pass; Wave 2 test file diagnostics resolved.
- **Migration 020**: `telegram_pairing` table for dashboard-initiated deep-link pairing.
- **Migration 021**: `tracked_authors` NULLS NOT DISTINCT uniqueness constraint.

## [1.2.1] - 2026-04-11

### Post-Audit Hotfix — Round 5 findings (F1 + F2)

Six production-blocking bugs fixed and nine HIGH-priority hardening items addressed following the Round-5 deep audit of Phase 1 Discovery & Pulse. All fixes verified against the audit report at `docs/plans/2026-04-11-deep-audit-round5-report.md`.

#### Critical bug fixes (F1)

- **Weekly Summary SQL column** (`weekly_summary.py`) — The engagement subquery used `pus.user_state IN ('saved', 'reading', 'read')`. The actual schema column is `status` and the starred state is `'starred'`. Resulted in 500 on every weekly digest request. Fixed column name and value.
- **Stage-2 LLM scoring 100% failure** (`pulse/scoring.py`) — `_LLM_MODEL = "fast"` (qwen3.5:4b) is a thinking model that fills its `<think>` block with the full chain-of-thought when `_LLM_MAX_TOKENS = 256`, leaving no budget for JSON output. `strip_think_blocks()` left an empty string; `json.loads("")` raised on every call. Fixed by switching to `"smart"` (mistral-nemo, non-thinking) and raising max_tokens to 512.
- **PubMed XXE hardening** (`sources/pubmed_source.py`) — `etree.fromstring()` with the default lxml parser resolved external entities and fetched network DTDs. Replaced with a hardened `etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)`. Added XXE regression test `tests/test_source_pubmed_xxe.py`.
- **Config validation** (`routers/settings.py`) — `PUT /api/config/pulse.cron` accepted any string silently. Added per-key validator dict: cron via `CronTrigger.from_crontab()`, weights shape + value range, deck_size/stage2_top_k positive-int check. Invalid values now return 400. Frontend cron input gains `isValidCron()` guard and inline error text.
- **Author-bonus dual-set fix** (`pulse/profile.py`, `pulse/scoring.py`) — `UserProfile.tracked_author_ids` stored opaque S2 IDs; stage-1 compared them against display names → 0% match rate → 15% of ranking power dead. Split into `tracked_author_names` (lowercased display names) and `tracked_author_s2_ids` (S2 IDs). Stage-1 now does dual-set intersection.
- **`persist_deck` card_count divergence** (`pulse/deck.py`) — `card_count` was set to `len(cards)` before cards were inserted. If a `papers` row was missing, `INSERT … SELECT … WHERE external_id=$X` returned 0 rows silently, so card_count was always too high. Refactored to insert deck with count=0, use `RETURNING id` per card, count successes, then UPDATE. Wrapped in a transaction.
- **Telegram `_simple_digest` engagement filter** (`telegram_bot/orchestration/paper_digest.py`) — fallback digest fetched ALL papers from the last 7 days with no engagement gate. Added `WHERE EXISTS` subquery matching starred/reading/read state or positive pulse rating. Keeps the fallback consistent with Model C.

#### HIGH-priority hardening (F2)

- **Source rate-limiting** — PubMed and OpenAlex source plugins now mirror the arxiv_source rate-limiter pattern (`asyncio.Lock` + `time.monotonic()`). PubMed: 3 req/s free, 10 req/s with API key. OpenAlex: 9 req/s.
- **Broadened HTTP exception catch** — both PubMed and OpenAlex plugins now catch `httpx.HTTPError` (parent class) instead of `httpx.HTTPStatusError` only, covering `RequestError`, `TimeoutException`, and `ConnectError`.
- **OpenAlex mailto parameter** — correctly sends `mailto=<OPENALEX_EMAIL>` query param for the polite pool. API key is sent as a separate `Authorization: Bearer` header.
- **Discovery source cache** (`pulse/discovery.py`) — `discover_candidates` now accepts an optional `source_cache` dict and reuses singleton source instances instead of constructing fresh instances (which reset rate-limiter state). Callers pass `app.state.sources`.
- **Scheduler starts pulse job independently** (`scheduler.py`, `main.py`) — the pulse overnight job now starts when `pulse.enabled=true` even when `AUTO_FETCH_INTERVAL_HOURS=0`. Previously, both jobs were gated on the interval.
- **Live cron reschedule** (`routers/settings.py`) — `PUT /api/config/pulse.cron` now immediately calls `scheduler.reschedule_job("pulse_overnight", trigger=CronTrigger.from_crontab(value))` via `request.app.state.scheduler`.
- **`pdf_resolutions` NULLS NOT DISTINCT** (`db/migrations/019_pdf_resolutions_nulls_not_distinct.sql`) — The unique constraint `(doi, arxiv_id)` previously treated every NULL pair as distinct (PostgreSQL default), making the cache ineffective for papers with only one identifier. Migration 019 adds `NULLS NOT DISTINCT`.
- **Telegram `/pulse_now` command** — registered in `handlers/command_handler.py`; posts to `/api/pulse/generate` and replies with outcome.
- **Telegram HTML URL escaping** — `format_paper_card` now passes paper URLs through `html.escape(url, quote=True)`.
- **Per-route error boundaries** (`frontend/src/App.tsx`) — all 12 routes are now wrapped in `<RouteErrorBoundary>` to contain render errors to the current route.
- **Rate mutation UX** (`PulseDeck.tsx`, `PulseCard.tsx`) — removed spurious `pulse-today` invalidation on rate; rated card IDs tracked in local state to disable buttons and show chosen rating.
- **Topic description max length** — `TopicCreate.description` / `TopicUpdate.description` gain `max_length=1000` validation; frontend textareas gain `maxLength={1000}`.
- **Pyright: resolver unused parameters** — `_try_arxiv` and `_try_unpaywall` unused parameters renamed with `_` prefix.
- **Test isolation** — `test_pulse_scheduler.py` no longer stubs `apscheduler.triggers.cron` in `sys.modules` (it was poisoning `CronTrigger.from_crontab()` for all downstream tests in the same session). `conftest.py` pre-imports the real module to anchor it before collection-time stubs can replace it.

## [1.2.0] - 2026-04-11

### Phase 1 Shipped — Discovery & Pulse subsystem

The overnight proactive paper discovery subsystem is complete. All six implementation layers landed, all acceptance criteria were met, and the eval harness passes (precision@10 = 100%, no-leakage = 0% on the synthetic labeled set). The full architectural design is embedded in `docs/PRD.md` §3.1.1 and §8.5 and in `AGENTS.md` under "Discovery & Pulse Subsystem".

- **Discovery & Pulse subsystem** — a proactive overnight paper discovery layer. An APScheduler job runs overnight, polls enabled external sources in parallel, scores candidates with a hybrid embedding + LLM pipeline, and persists a small curated card deck for morning delivery via the My Day widget and optional Telegram.
- **New source plugins** — OpenAlex (new plugin, free key required, cross-domain coverage) and PubMed (new plugin, enabled by default, optional key for rate limit upgrade). Both implement the extended `PaperSource` ABC with two new optional methods (`fetch_new_since`, `get_recommendations`). Existing arXiv and Semantic Scholar plugins are extended to implement the same methods.
- **PDF resolution chain** — `pulse/resolver.py` tries arXiv direct → Unpaywall fallback → cached failure marker. Called lazily when a Pulse card is saved. Also usable as a fallback in the existing ingestion pipeline when S2's PDF URL is broken.
- **Scoring pipeline (3 stages in Phase 1)** — Stage 1 embedding similarity filter (library centroid + topic embeddings + recency decay), Stage 2 LLM relevance and novelty scoring on top 50 candidates using the local Ollama fast model, Stage 3 weighted combination with author-match bonus. Rating feedback is collected from day one in the new `pulse_ratings` table; Phase 2 will add a Stage 4 per-user logistic regression classifier consuming that data.
- **"Why this paper?" transparency popover** on every Pulse card, showing matched topics, matched authors, per-signal score breakdown, and the LLM's one-sentence reasoning.
- **Telegram delivery rewrite** — `services/telegram_bot/app/orchestration/research_pulse.py` was gutted from ~164 lines of naive keyword-search-and-notify to ~94 lines of thin delivery over `GET /api/pulse/today`. All scoring/processing logic now lives in the `pulse/` package in paper_ingestion. Inline 👍/👎/💾 rating callbacks wired to `POST /api/pulse/rate`.
- **New database tables (migration 018)** — `pulse_decks`, `pulse_cards`, `pulse_ratings`, `pdf_resolutions`. Plus one new optional column `topics.description TEXT NULL`. Plus new `paper_sources` rows for `openalex` and `pubmed`. Plus new `user_config` entries seeding Pulse defaults (`pulse.enabled`, `pulse.cron`, `pulse.deck_size`, `pulse.stage2_top_k`, `pulse.weights`).
- **New backend package** `services/paper_ingestion/app/pulse/` containing `job.py`, `profile.py`, `discovery.py`, `scoring.py`, `prompts.py`, `deck.py`, `resolver.py`, `__init__.py`.
- **New API router** `services/paper_ingestion/app/routers/pulse.py` exposing six endpoints: `POST /api/pulse/generate`, `GET /api/pulse/today`, `GET /api/pulse/history`, `POST /api/pulse/rate`, `GET /api/pulse/explain/{card_id}`, `GET /api/pulse/stats`.
- **New frontend components** — `PulseDeck` (My Day widget), `PulseCard` (card component), `WhyPopover` (transparency dialog), and a reusable `InfoTooltip` primitive usable across all Settings sections going forward.
- **Settings extensions** — optional `description` field on Topics, Pulse enable/time-picker in Automation, API-key fields and provider tooltips in Sources, scoring weight sliders in the renamed "Pulse & Recommendations" section.
- **New optional environment variables** — `OPENALEX_API_KEY`, `PUBMED_API_KEY`, `UNPAYWALL_EMAIL`. All optional; Pulse runs with graceful degradation for whatever is provided.
- **Phase 1 new dependency** — `lxml` for PubMed XML parsing. `scikit-learn`, `networkx`, and `bertopic` are deferred to Phase 2 to keep the Phase 1 footprint minimal.
- **Graceful degradation** — five failure modes exercised by `test_pulse_degradation.py`: empty profile, all sources disabled, Stage 2 LLM timeout, zero candidates after Stage 1, Qdrant unavailable.
- **Scheduler wiring** — APScheduler gains one new `pulse_overnight` job in `scheduler.py`; no new scheduling mechanism.
- **Eval harness** — `scripts/eval_pulse.py` runs the full scoring pipeline against a labeled 30-paper synthetic set (10 yes / 10 maybe / 10 no). Current run: precision@10 = 100%, no-leakage = 0% (targets: ≥60% and ≤10% respectively). PASS.
- **Playwright E2E** — `frontend/e2e/pulse.spec.ts` covers the happy-path deck render, rating interaction, and "Why this paper?" popover.
- **Testing discipline** — TDD-strict per the brainstorm decision (Axis 2 = C). Tests were written before implementation for every function in `pulse/`. Source plugins tested with recorded offline fixtures.

**Anti-bloat budget (spec §10):** 11 new Python files (8 pulse package + 2 source plugins + 1 new API router) / 4 new frontend files / 1 new migration / 0 new Docker services / 0 new pages. The router file is the one "+1" over the pulse-package ≤10 limit called out in the spec.

**Sprint diff totals:** 78 files changed, 11,234 insertions, 342 deletions across backend, frontend, telegram bot, docs, migrations, and tests.

### Added
- **My Day page**: daily productivity command center with Pomodoro timer, quick-add tasks, project badges, Project Pulse widget
- **Pomodoro timer**: wall-clock based timing with pause/resume, auto-logging of completed sessions, browser notifications, configurable durations
- **Timer settings**: configurable work/break durations and cycle count in Settings page
- **Quick-add tasks**: create tasks from My Day with optional project assignment
- **Project badges**: clickable project labels on tasks linking to Projects page
- **Collapsed empty cards**: Learning/Recommended section collapses to compact row when both empty
- **Recommendation engine** (Phase 1): score-based paper recommendations with liked-paper, project-relevance, and recency signals

### Fixed
- Migration 015 idempotency guard (conditional NOT NULL constraint)
- Migration runner advisory locking (pg_advisory_lock)
- QuickAddTask/TaskList error handling (onError callbacks)
- Vite dev proxy for /api/executive routes
- Backend focus session duration validation (gt=0, le=24)
- Pomodoro auto-logging: completed work sessions now logged automatically
- Timer state persistence: survives page refresh
- Timer accuracy: wall-clock based, no drift in background tabs
- Rate limits on get_my_day (60/min) and log_focus_session (10/min)
- HTTPException inside transaction causing silent rollback in log_focus_session
- QuickAddTaskRequest validation (title 1-500, priority 1-4)
- Recommendations endpoint unbounded limit (now ge=1, le=200)
- Pipeline HTTP timeouts (120-180s for PDF operations)
- Focus command duplicate timer jobs
- Cross-encoder reranker CUDA fallback to CPU
- Reranker lru_cache permanent None caching
- init.sql sync with migrations (paper_recommendations, search_vector)
- Docker learning_engine dependency ordering

## [1.1.0] - 2026-03-08

### Added
- React 19 dashboard replacing Streamlit (Vite + Shadcn/ui + TanStack Query + Zustand)
- Research Feed with filtering, sorting, and paper detail pages
- Citation graph visualization (Cytoscape.js)
- Knowledge graph visualization (Cytoscape.js)
- Structured extraction table with templates
- FSRS spaced repetition for learning cards
- Analytics page with activity, retention, and review charts
- Project management with tasks, milestones, and papers
- Settings page for topics, sources, authors, ingestion, automation, extraction, recommendations
- nginx reverse proxy for frontend routing
- CORS middleware for both FastAPI services
- SSE streaming for paper analysis and RAG chat

## [1.0.0] - 2026-02-15

### Added
- Paper ingestion from arXiv, Semantic Scholar, and manual PDF upload
- Hybrid search (BM25 + semantic) with cross-encoder reranking
- RAG-powered Q&A with citation verification
- Telegram bot with push notifications and daily briefings
- PostgreSQL database with 13 migrations
- Qdrant vector database for semantic search
- LiteLLM for model-agnostic LLM access
- Docker Compose deployment (9 services)
