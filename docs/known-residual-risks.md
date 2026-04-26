# Known Residual Risks

This document tracks acknowledged-but-deferred risks. Each entry links the originating audit ID and the rationale for deferring the full fix.

## PI-EDGE-002 / PI-EDGE-004 — paper-ownership row enforcement deferred to Phase 2

**Audit findings:** PI-EDGE-002 ([routers/jobs.py:72-111]), PI-EDGE-004 ([routers/notes.py:153-220]).

**Current state:** input-shape validation is enforced via discriminated-union Pydantic payloads and a handler-side idempotency guard. Job rows are tagged with `user_id` (single-user mode → NULL) and read-side filters honor it. Note rows are not user-scoped because `paper_notes` and `papers` lack a `user_id` column.

**Why deferred:** the full multi-tenant ownership fix requires a `papers.user_id` migration plus a multi-tenant model; both are out of scope for the Sprint 3 remediation sprint. The current single-user deployment makes this latent rather than active.

**Reopen criteria:** when `papers.user_id` migration lands OR when multi-tenant mode is enabled.

---

## DOCKER-001 / DOCKER-002 / DOCKER-005 — Secret env vars not yet `_FILE`-mounted for all consumers

**Audit findings:** DOCKER-001 (`POSTGRES_PASSWORD` in `DATABASE_URL`), DOCKER-002 (`LITELLM_MASTER_KEY`), DOCKER-005 (`PGPASSWORD` in backup sidecar).

**Current state:** `JARVIS_API_KEY` already uses Docker Secrets correctly. The Postgres container uses `POSTGRES_PASSWORD_FILE`. However, `DATABASE_URL` is assembled from `${POSTGRES_PASSWORD}` in the `shared-env` anchor, exposing it via `docker inspect` and `/proc/<pid>/environ`. Same for `LITELLM_MASTER_KEY` and `PGPASSWORD`.

**Why deferred:** n8n supports `DB_POSTGRESDB_PASSWORD_FILE` but that requires additional compose wiring; asyncpg does not natively support DSN-from-file (needs a wrapper); LiteLLM does not expose a `_FILE` variant for `LITELLM_MASTER_KEY` (needs upstream support or an entrypoint wrapper); `backup.sh` needs a `.pgpass`-via-Docker-Secret wrapper. All three require coordinated changes across multiple service entrypoints.

**Acceptable risk for:** single-user LAN deployment where no untrusted users share the Docker host.

**Reopen criteria:** before exposing JARVIS to shared infrastructure (cloud VM, lab host with multiple users) or when LiteLLM adds `_FILE` support.

---

## TG-001 / TG-002 / TG-003 — Telegram callback hardening

**Audit findings:** TG-001 (`paper_bookmark_callback` writes directly to DB pool), TG-002 (review callbacks lack `@rate_limit`), TG-003 (`start_review_callback` unsafe `update.message` patch).

**Current state:** TG-001 bypasses the API tier (no audit log, no ownership check, schema drift will silently break it). TG-002 allows unlimited `rate_*` POSTs from the bot's source IP. TG-003 patches `update.message = query.message` without checking for `InaccessibleMessage`.

**Why deferred:** All three require focused Telegram bot sprint work. No active exploitation risk on single-user instances (the bot token is secret and the only paired user is the owner).

**Reopen criteria:** when a dedicated Telegram hardening sprint is planned, or if the bot is exposed to a multi-user Telegram group.

---

## PI-EDGE-010 — Per-tick NOTIFY listener creation

**Audit finding:** PI-EDGE-010 (`_wait_for_job_notification` creates a new `asyncpg-listen` listener per SSE poll tick).

**Current state:** a 60-second job with 2-second polls generates 30 connection cycles per stream. No correctness impact; only a connection-churn smell.

**Why deferred:** no observed connection exhaustion at current job volumes (single-user). Fix requires refactoring the SSE stream to hold a single long-lived listener per job stream — moderate blast radius.

**Reopen criteria:** when job volume exceeds ~50 concurrent streams, or when connection pool exhaustion is observed in logs.

---

## Wave 5 audit items (ARCH-001/002, DRY-001/002/003, GOD-001, SYM-001, COMPLIANCE-001/002, DOCKER-003)

**Audit findings:** architecture/DRY/symmetry items from the 2026-04-26 audit, all in the "Wave 5 — Architecture / DRY" fix wave.

**Current state:** ~400 LOC removable with zero behaviour change. Items include: inverted shim comments (ARCH-001/002), duplicated jobs router (DRY-001), duplicated `main.py` boilerplate (DRY-002), `crypto` not re-exported (DRY-003), `routers/search.py` 1021 LOC (GOD-001), source plugin HTTP error divergence (SYM-001), `QuoteVerifier` instantiated per-request in notes router (COMPLIANCE-001), `extraction/jobs.py` 9-line shim (COMPLIANCE-002), Qdrant/Ollama raw TCP healthchecks (DOCKER-003).

**Why deferred:** all are tech-debt with no correctness or security impact. Bundled as a dedicated tech-debt sprint to avoid merge conflicts with feature work.

**Reopen criteria:** when a tech-debt sprint is scheduled (recommended after the next feature release).

---

## WS-7 Hermes spike — build-vs-adopt unresolved

**Context:** WS-7 of the post-R14 roadmap targets a conversational agent layer.

**Current state:** not started. The build-vs-adopt decision (NousResearch Hermes fork vs LiteLLM native tool-calling) requires explicit human sign-off before the spike is worth running.

**Deferral rationale and decision criteria:** see `docs/plans/2026-04-26-ws7-hermes-deferral.md`.

**Reopen criteria:** human reviewer selects build vs adopt path from the decision criteria table in that doc.

---

## WS-2.3 `paper_summaries.themes_verified` — descoped

**Context:** the closeout plan (Wave 2B) originally proposed a `themes_verified BOOL` column on `paper_summaries`.

**Current state:** weekly response already carries `verified_themes` and `unverified_themes` in-memory. The persisted column adds storage without any UI consumer querying it.

**Why descoped:** no consumer demand identified. Migration would add schema complexity without enabling any new feature.

**Reopen criteria:** if a UI widget or API endpoint needs to filter summaries by `themes_verified` status, at which point design dedicated `weekly_digest_runs` / `weekly_digest_topics` tables rather than a bare boolean column.

---

## DB-003 (cosmetic BEGIN/COMMIT) and DB-004 (auto-update trigger) — tech-debt sprint

**Audit findings:** DB-003 (`038_paper_contradictions.sql` missing `BEGIN`/`COMMIT`; cosmetic since runner wraps in `conn.transaction()`), DB-004 (`updated_at` columns on 5 tables lack `BEFORE UPDATE` trigger).

**Why deferred:** DB-003 is a maintenance hazard only for manual `psql` replay; live runs are atomic. DB-004 requires a schema-wide trigger function and `ATTACH TRIGGER` on 5 tables — low risk in current single-writer model.

**Reopen criteria:** next schema-wide migration that touches these tables, or if manual `psql` replay of migrations is added to the ops runbook.

---

## PI-EDGE-005 wall-clock budget — deferred until profiling shows need

**Audit finding:** PI-EDGE-005 partial — cross-ref pre-filter for contradiction candidate pairs shipped (Wave B2). The remaining sub-item is an outer wall-clock timeout + `asyncio.gather` concurrency for `_classify_candidate` LLM calls.

**Current state:** with cross-ref pre-filter, the O(n²) pair space is reduced significantly for typical library sizes. LLM calls still run sequentially.

**Why deferred:** wall-clock budgeting adds complexity (cancellation, partial-result semantics); sequential LLM calls are predictable. No observed timeout on current library sizes.

**Reopen criteria:** when contradiction scan wall-clock exceeds 60 seconds on real data, measured by adding a timer log in `scan_contradictions`.
