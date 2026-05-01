# Known Residual Risks

_Last updated: 2026-04-30_

This document tracks acknowledged-but-deferred risks. Each entry links the originating audit ID and the rationale for deferring the full fix.

Related docs:

- [../AGENTS.md](../AGENTS.md) - when agents must consult this file.
- [AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md) - how residual risks affect planning and closeout.
- [ARCHITECTURE.md](ARCHITECTURE.md) - runtime boundaries affected by residual risks.
- [plans/2026-04-29-refreshed-desloppify-score-work.md](plans/2026-04-29-refreshed-desloppify-score-work.md) - current manual
  score-work plan after the Desloppify refresh.

## DESLOPPIFY-TRIAGE-001 — review issue IDs cannot pass observe confirmation

**Current state:** the 2026-04-29 Desloppify refresh imported 62 current review
issues, and the staged observe runner analysed them. Confirmation failed because
Desloppify's observe citation parser only accepts issue IDs shaped like
`review::deadbeef` or bare hex suffixes, while current review IDs are
descriptive strings such as `review::.::holistic::type_safety::...`.

**Impact:** staged triage can produce useful output, but the confirmation gate
cannot be completed for the refreshed review queue without a Desloppify parser
fix or a changed issue-ID format.

**Mitigation:** keep the run artifacts under `.desloppify/triage_runs/` and use
[docs/plans/2026-04-29-refreshed-desloppify-score-work.md](plans/2026-04-29-refreshed-desloppify-score-work.md) as the current
manual score-work plan.

**Reopen criteria:** before relying on Desloppify staged triage as an automated
gate again, update the parser to accept descriptive review IDs and rerun
`desloppify plan triage --run-stages --runner codex`.

---

## PI-EDGE-002 / PI-EDGE-004 — paper-ownership row enforcement — CLOSED (Sprint 4)

Fixed in commits 9fea1b6 + 0285f05. Migration 042 added `papers.user_id`; jobs, notes, papers, rag, extractions, search, feed, and discovery routes now enforce row-level ownership. Remaining multi-tenant follow-ups tracked below under Sprint 4 deferrals.

---

## DOCKER-001 / DOCKER-002 / DOCKER-005 — Secret env vars — CLOSED (Sprint 4)

Fixed in commit 9fa6161. DOCKER-001: `POSTGRES_PASSWORD` moved to Docker Secret; `DATABASE_URL` assembled from `_FILE`-mounted secret. DOCKER-002: `LITELLM_MASTER_KEY` moved to Docker Secret via entrypoint wrapper. DOCKER-005: `PGPASSWORD` in backup sidecar replaced with `.pgpass`-via-Docker-Secret. No longer deferred.

---

## TG-001 / TG-002 / TG-003 — Telegram callback hardening — CLOSED (Sprint 4)

Fixed in commit 15491a8. TG-001: `paper_bookmark_callback` replaced with API-tier call. TG-002: review callbacks decorated with `@rate_limit`. TG-003: `start_review_callback` guards against `InaccessibleMessage`. No longer deferred.

---

## PI-EDGE-010 — Per-tick NOTIFY listener creation

**Audit finding:** PI-EDGE-010 (`_wait_for_job_notification` creates a new `asyncpg-listen` listener per SSE poll tick).

**Current state:** a 60-second job with 2-second polls generates 30 connection cycles per stream. No correctness impact; only a connection-churn smell.

**Why deferred:** no observed connection exhaustion at current job volumes (single-user). Fix requires refactoring the SSE stream to hold a single long-lived listener per job stream — moderate blast radius.

**Reopen criteria:** when job volume exceeds ~50 concurrent streams, or when connection pool exhaustion is observed in logs.

---

## Wave 5 audit items — CLOSED (Sprint 4)

ARCH-001/002, DRY-001/002/003, GOD-001, SYM-001, COMPLIANCE-001/002, DOCKER-003, DB-003, DB-004, SYM-002, ARCH-003 were all fixed in Sprint 4 (commits b14bba2, 19be425, 34a06a6, 996ebe6, 72577e1, f5a2c21, cfef5dc, 0cff746). No longer deferred.

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

## DB-003 (cosmetic BEGIN/COMMIT) and DB-004 (auto-update trigger) — CLOSED (Sprint 4)

Fixed in commits 72577e1 (DB-003: `BEGIN`/`COMMIT` added) and 0cff746 (DB-004: `set_updated_at` trigger added in migration 042). No longer deferred.

---

## PI-EDGE-005 wall-clock budget — deferred until profiling shows need

**Audit finding:** PI-EDGE-005 partial — cross-ref pre-filter for contradiction candidate pairs shipped (Sprint 3 Wave B2). The remaining sub-item is an outer wall-clock timeout + `asyncio.gather` concurrency for `_classify_candidate` LLM calls.

**Current state:** with cross-ref pre-filter, the O(n²) pair space is reduced significantly (~95%) for typical library sizes. LLM calls still run sequentially.

**Why deferred:** wall-clock budgeting adds complexity (cancellation, partial-result semantics); sequential LLM calls are predictable. No observed timeout on current library sizes.

**Reopen criteria:** when contradiction scan wall-clock exceeds 60 seconds on real data, measured by adding a timer log in `scan_contradictions`.

---

## Sprint 6 deferrals (2026-04-28)

### M1 — `pulse_ratings` and `paper_user_state` null `user_id` backfill

**`pulse_ratings` and `paper_user_state` writes do not stamp `user_id`.** The `current_user_id_or_none` stub returns None today, so rows accumulate with `user_id=NULL`. Before enabling multi-tenant, a backfill migration must assign all NULL `user_id` rows to a sentinel system user. Tracked as M1 in 2026-04-28 audit.

**Reopen criteria:** before `MULTITENANT_ENABLED=true` is enforced with a real auth resolver.

### H5 — Migration 043 live-fixture test deferred

Migration 043 uses defensive PL/pgSQL constraint-name lookup (Sprint 6 fix). The live-fixture migration test is deferred to a future sprint with proper test infra.

**Reopen criteria:** when a migration test harness with a real ephemeral Postgres instance is available.

### L1 — Frozenset whitelist for `extra_sets` not enforced

Frozenset whitelist for `extra_sets` not yet enforced — current guard is `isinstance(s, str)`. Treat all callers as trusted. A future hardening pass should add an allowlist of known-safe set names.

**Reopen criteria:** if `extra_sets` accepts caller-controlled input from any untrusted surface.

### L4 — S3 backup encryption relies on bucket SSE only

S3 backup encryption relies on bucket SSE only — openssl pipeline ships in next infra sprint. Backups are encrypted at rest by S3 server-side encryption, but not client-side encrypted before upload.

**Reopen criteria:** when compliance requirements mandate end-to-end encryption of backup files.

### L3 — `nginx.conf` hardcodes the trusted Docker subnet `172.19.0.0/16`

`frontend/nginx.conf:17-19` declares `set_real_ip_from 172.19.0.0/16` so that the rate-limiter sees the real client IP rather than the nginx container IP. The subnet is hardcoded to the default Docker bridge JARVIS uses; if Docker assigns a different subnet to the `jarvis` network (e.g. when `default-address-pools` differ on the host or another stack reserved the range), the `real_ip_recursive` directive won't strip the proxy hop and rate-limit keys will land on the nginx container's IP — effectively a single shared bucket per service.

**Mitigation:** verify the bridge subnet at deploy time with `docker network inspect jarvis | jq '.[0].IPAM.Config[].Subnet'` and either match the host network or accept the rate-limit degradation.

**Reopen criteria:** when LAN/tunnel deployments observe rate-limit anomalies, or when the deploy story moves to a non-default Docker network configuration. Tracked as L3 in 2026-04-28 audit.

---

## Sprint 4 deferrals (2026-04-26)

### TG-004 — In-memory bot rate limits

Accepted for single-user single-bot LAN deployment. Distributed rate limiting
(Redis-backed) deferred until multi-bot or LAN-exposed scenarios materialize.

### SEC-106 — CSP style-src 'unsafe-inline'

Nonce-based CSP requires multi-day Vite plugin refactor (each style-injecting
component must accept a nonce; some 3rd-party libs don't). Deferred.

### SEC-DEP-001 — Full requirements pinning

Sprint 4 did not ship top-5 pinning — `requirements.txt` still uses `>=` floor
only across all services. Recommend pinning fastapi, pydantic, asyncpg,
qdrant-client, and sentence-transformers with floor+ceiling in the next sprint
to reduce supply-chain drift risk.

### PI-EDGE-005 — Wall-clock budget half

Cross-reference pre-filter (Sprint 3 WS-B2) addresses ~95% of cases.
Wall-clock budget enforcement deferred until profiling shows need.

### PI-EDGE-010 — Single long-lived NOTIFY listener

Performance smell, not a correctness issue. Defer to performance sprint.

### Cross-paper RAG ownership thread-through (Sprint 4 follow-up)

Wave 6B-β covered single-paper and cross-paper SQL endpoints, but the
`embedder.search_chunks_global()` retrieval path used by `/api/ask`,
`/api/ask/stream`, and `weekly_summary` doesn't receive `user_id`. Multi-user
mode would currently leak chunks across users for these 3 endpoints.
Recommended WS-6C ticket.

### Search upsert user_id stamping (Sprint 4 follow-up)

`POST /api/search` performs external-source fetch + DB upsert via
`pdf_workflow.upsert_paper`, which doesn't currently stamp `user_id` on the new
row. Multi-user end-to-end isolation requires this. Recommended follow-up.

---

## WS-AH / WS-AH2 — 2026-04-29/30 audit items — CLOSED (Sprint WS-AH2)

All items below were raised in the 2026-04-29 deep audit and closed in commits
`c6cfd14..6adef70` (WS-AH2, merged 2026-04-30).

| ID | Description | Closing commit |
|---|---|---|
| H2 | Hard-delete PG DELETE now commits inside transaction; Qdrant cleanup runs outside as best-effort try/except | c6cfd14 |
| M8 | `_assert_confirm_title_matches` strips whitespace from both sides before comparing | c6cfd14 |
| DRY-1 | `IS_ARCHIVED_SQL` predicate canonical constant propagated across `papers.py` (5 sites), `feed_query.py`, and `pulse/deck.py` | c6cfd14, 6e82103 |
| NI-1 | `pulse_decks` INSERT now explicitly binds `user_id` in the column list (was omitted, causing every deck to upsert into the NULL slot) | 6e82103 |
| NI-2 / M11 | `_simple_digest` accepts `db_user_id: int \| None` kwarg; all three subqueries bind it via `IS NOT DISTINCT FROM $1`; caller passes `db_user_id=None` with a multi-tenant TODO | 6785cc8 |
| L12 | Legacy `status='starred'` backfill covered by migration 046; one-line note added confirming no warning needed for post-backfill rows | 6785cc8 |
| NI-3 | `HardDeleteModal` mutation gains `onError` toast handler; backend rejections no longer silently freeze the modal | bc66ddb |
| H5 (bulk) | Bulk selection now clears via `useEffect([surface])` on every surface change including browser back/forward and deep-links, not only imperative chip-handler calls | ef7b3e0 |
| NI-5 | `app_factory` `zip(strict=True)` + eager length check enforce equal-length `init/teardown` contract; `None` padding supported for hooks without teardown | a65ede4 |
| NI-6 | Migration-lint script anchors its `cwd` to the repo root, not the caller's shell cwd | 6b8b83e |
**Still open from Sprint 6:** M1 (multi-tenant user resolver), zotero.remove handler, inbox auto-prune cron, trash auto-purge cron, bulk-dismiss-by-topic, saved-search alerts, and migration 046 transition guard retirement — all tracked in the Sprint 6 deferrals section above.

**Phase A — Paper Lifecycle Redesign — LANDED (2026-05-01, version 1.3.0):** the WS-AH2 sprint shipped on top of the *legacy* lifecycle schema (saved/dismissed/starred/archived booleans + status enum). [docs/specs/2026-04-29-paper-lifecycle-redesign.md](specs/2026-04-29-paper-lifecycle-redesign.md) collapsed that schema to a single `state` ENUM + orthogonal `starred` flag + separate `recommendation_feedback` table (migrations 047 + 048 + 049). The legacy contract docs `docs/specs/paper-lifecycle-contract.md` and `docs/specs/feed-information-architecture.md` were **deleted** (per spec §11 — clean cut, no deprecated stubs). The WS-AH2 fixes above are *preserved* (NEW-H2, NI-1, NI-2, NI-3, H5, NI-4, NI-5, NI-6) or *structurally superseded* (NEW-M8, DRY-1, L12) by the redesign — see redesign spec §15 for the full disposition table. Phase B sprints (Instructor / Langfuse / mxbai-rerank / Taskiq) follow; see the META plan at [docs/plans/2026-04-30-marathon-meta.md](plans/2026-04-30-marathon-meta.md).

---

## Falsified findings — 2026-04-29 audit

The deep-audit (commit af1af21) flagged two findings that direct re-Read against HEAD falsified:

### M5 — `safe_for_prompt(mode="strip")` does NOT silently pass BIDI

**Audit claim:** `_strip_bidi_zw` is only called in the `escape` branch, so `mode="strip"` leaks BIDI control chars.

**Falsification:** `_CTRL_RE` (`libs/jarvis_common/jarvis_common/prompt_safety.py:13-15`) explicitly includes the BIDI ranges `‪-‮⁦-⁩`. The `_CTRL_RE.sub(...)` call runs in BOTH modes. The `_strip_bidi_zw(text)` call inside the escape branch was therefore a redundant no-op (removed in this sprint as dead-code cleanup) — not a security gap.

### M13 — Telegram `paper_save_callback` body `{"star": False}` does NOT un-star

**Audit claim:** posting `{star: false}` to `PUT /save` un-stars previously-starred papers.

**Falsification:** `services/paper_ingestion/paper_ingestion/routers/papers.py:705-706` — `if body.star: extra["starred"] = True`. The backend ONLY writes `starred=TRUE` when truthy; `star=False` is a no-op for the starred flag. Existing payload is correct.

These records exist so future auditors don't re-raise the same falsified findings.
