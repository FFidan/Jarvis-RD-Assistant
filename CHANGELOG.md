# Changelog

All notable changes to JARVIS RD Assistant are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).
## [unreleased]

### Cleanup & footprint reduction (2026-06-01)

Removed the optional n8n workflow-automation integration. APScheduler (built into the Telegram bot) remains the built-in scheduler and covers all daily-briefing and review-reminder scheduling, so n8n was redundant. Also pruned dead developer scripts, superseded benchmark fixtures, and an obsolete PDF-engine test, and refreshed the documentation for accuracy (version strings, the FastAPI pin, the conversational-agent roadmap gate) while trimming internal operational detail from the published site.

### Dependency security & PDF extraction (2026-05-29)

The PDF text-extraction engine was migrated from marker-pdf to **Docling**. This closed the last dependency CVEs that marker-pdf's transitive version caps had been blocking — Pillow (6 advisories), the `diskcache` transitive (dropped via instructor 1.15), and a transformers code-execution advisory (fixed in 5.x) — and moved openai to the 2.x line. It also made citation **page numbers exact**: chunks are now bounded to a single page using Docling's per-page provenance, so a cited page always matches the page snapshot the reader sees.

### Post-public-readiness audit (2026-05-26)

A six-week internal audit-and-remediation pass closed roughly 120 findings ahead of the first public release. The themes below capture user-visible and operator-visible changes; commit-level detail follows in the per-area sections.

**Security.** The background-job Server-Sent Events stream now requires an authenticated session — it previously accepted unauthenticated subscriptions and returned job state for the NULL user. All cross-user data paths were re-audited: project recommendations, paper-source feedback, author alerts, and review-deck queries are now scoped to the logged-in user, and admins cannot read other users' research data. Prompt-injection vectors in PDF body text, paper titles, discovery snippets, and tracked-author bios are stripped before reaching the model with a documented prompt-shape contract enforced by an AST check. Container processes drop privileges, run with `no-new-privileges` set, and ship with a root-level `.dockerignore` so secret files and host-bound paths cannot accidentally land in the build context. Lock-file integrity (Python `uv.lock`, npm `package-lock.json`) is now verified against registry pins at install. Append-only audit logs reject `UPDATE`/`DELETE` at the PostgreSQL rule level, and the pairing-code length, rating regex, and ProjectManager method signatures were tightened against malformed input.

**Correctness.** Several long-standing cross-tenant bugs were fixed: the recommender's project query now filters by `user_id`; author-alert dedupe is per-user instead of global; the Zotero push flow no longer leaks `paper_id` across sessions. A handful of API surfaces that previously returned 200 with inconsistent envelopes now return one shape, and the streaming Chat error path surfaces transport errors to the UI instead of swallowing them. The `paper_sources` table and `PaperSource` abstract base were brought into symmetry so the catalog the UI shows matches what the ingestion job actually runs.

**Architecture.** Two oversized modules were split by responsibility: `entities.py` (814 LOC) became a typed router + a Postgres adapter + a Qdrant adapter; `routers/settings.py` was decomposed by settings domain. A new internal Telegram bot API removes the previous Telegram-bot → paper-ingestion DB-coupling. The migration history was squashed: the 88-file pre-v0.5.0 chain became a single `db/init.sql` bedrock with new migrations starting at `0089`, and `tests/test_baseline_invariants.py` pins the schema invariants.

**Developer experience.** Continuous integration now enforces type-check (Pyright zero errors), a test-shape contract (each test belongs to one of four documented shapes), the LLM prompt-shape AST check, and PII / burned-secret allowlists. The CI workflow was migrated to `astral-sh/setup-uv@v6` with a Python 3.12 pin and `uv sync --frozen`, cutting wall-clock from 8–15 minutes to 4–5 minutes. A pre-commit hook runs the same gates locally.

**Public-launch preparation.** This release ships a rewritten README with above-the-fold product screenshots, a Highlights section, and the four-audience deployment path; weekly `dependabot` updates for pip, npm, Docker base images, and GitHub Actions; structured GitHub issue templates (bug report, feature request) with security reports routed to a private GitHub Security Advisory; and a root `SECURITY.md` pointing to the threat model.

### Upgrade Notes

- **Migration baseline squashed.** The 88-file migration chain prior to v0.5.0 was consolidated into `db/init.sql` as the single baseline; new migrations start at 0089. The migration runner detects squashed-init state and applies forward without interruption — operators upgrading from v0.4.1 or earlier need no manual intervention. See `tests/test_baseline_invariants.py` for the schema invariants pinned.

### Security
- Cross-tenant project leak in recommender (`SEC-XTENANT-1`): `_refresh_recommendations_for_user` now scopes the projects query to `user_id`.
- Append-only `audit_log` (`SEC-AUDIT-1` / migration `0090_audit_log_append_only.sql`): blocks `DELETE`/`UPDATE` on the audit table via PG rule.
- Per-user author-alert dedupe (`SEC-AUTHALERT-1` / migration `0091_author_alert_log_user_dedupe.sql`): `ON CONFLICT (user_id, tracked_author_id, paper_id)` prevents cross-user alert suppression.
- Owner-override guard tests + audit-log emission (`SEC-OWNER-1` / `SEC-OWNER-2`).
- Pairing-code length bound (`SEC-RC-1`), rate-card regex (`SEC-RATING-1`), mandatory `user_id` on 3 ProjectManager methods (`SEC-PRJMGR-1`).

### Bug Fixes
- Email verification flag now respects SMTP exception path (`BUG-EMAIL-1`).
- Zotero BYTEA decode uses `crypto.resolve_secret_row` (memoryview-safe) (`BUG-ZOTERO-1`).
- Summarizer HTTPException propagation guarded at `paper_jobs.py:231, 270` (`BUG-SUMMARIZER-1`).
- Unread guard in `feed_query.py` resolves contradictory WHERE composition (`BUG-FEED-1`).
- `vector_writer` role boot-time password drop guard (`BUG-DBINIT-1`).
- Three missing CI smoke secrets (`BUG-CISMOKE-1`).
- `arxiv_source.py` parses `response.content` (bytes) instead of re-encoding `response.text` (`CFG-XML-1`).
- `weekly_summary.py` ThemeOutput stays as Pydantic instance, no dict.get on LLM output (`CFG-LLMOUT-1`).
- `pulse/job.py` degraded_reason OR-chain preserves earlier value (verified no change needed).
- 13 additional `CFG-*` MEDIUM fixes (config validation, GDPR scoping, dynamic model field-name validation, CIDR cache, etc.).
- 9 FRONTEND error-sentinel + per-tab error handling fixes (`FE-IDLE-1`, `FE-TRIAGE-1`, `FE-CHAP-1`, `FE-MD-1`, `FE-CP-1`, `FE-REVMODE-1`, `FE-DM-1`, `FE-API-1`, `FE-SSE-1`).
- 24 cross-cutting fixes: `decompose_query` doc catalog, sentry-init helper, fixture deduplication, `LockNotAvailableError` simplification, jobs throttle elapsed-seconds, `faux_qdrant` dim-mismatch + null-field guards, email format → replace, `_retry_after_seconds` cap at 3600, `_HAS_QWEN3` guard, `ScoredCandidate` frozen, scheduled `magic_link_tokens` purge, `SourceType.ZOTERO` enum, init-secrets.sh dedupe, profile.sh portable compose ps, TS-08 carve-out registry enforcement (`F-*`, `BE-*`).

### Hardening
- CI-CROSS-USER-FLAKY-1 mitigation: `_spin_pg_container` adds post-`pg_isready` TCP socket probe (30s deadline + 250ms retries) to eliminate SSL-init race that produced `ConnectionResetError [Errno 104]` on GitHub Actions runners.

### Deferred / Documented
- `## TELEGRAM-INTERNAL-API-1`, `## ARCH-AUTH-1`, `## ARCH-ENTITIES-1`, `## INFRA-INGEST-1`, `## CI-CROSS-USER-FLAKY-1` added to `docs/known-residual-risks.md` with reopen criteria.
- `OBS-1-RESIDUAL` git-history secret-purge remains operator-deferred (runbook at `docs/SECURITY.md:165-210`; repo private).

### Bug Fixes
- Narrow APIRoute for route-path assertions
- Owner-override resolver on Telegram-reachable LE endpoints + accurate cross-service boundary doc
- Split session-expiry side-effect out of render body (FE-1)
- Register AuthVerifyPage redirect timer with useEffect cleanup (L-4)
- Warn on skipped malformed SSE frames instead of silent drop (L-5)
- Short gcTime for sensitive admin/logs/config queries (FE-D)
- Model_validate for dynamic-key ThreadUpdate construction (M-4 review follow-up)
- Retain recent undated papers instead of silently dropping them (M-9)
- UTC date for recency decay in stage1 scoring (M-8)
- Collapse gap positions so reconstructed abstracts are single-spaced (M-10)
- Real ChunkForEmbedding + proper qdrant mock in M-3 tests (PI-8 review follow-up)
- Single-transaction cooldown-check+claim closes the interleave race (M-2 review)
- Justified type-ignore for deliberate-None negative test (M-1 review follow-up)
- Type-honest _filter_unread user_id in recommender tests (M-1 review)
- Deterministic Qdrant point ids prevent orphan/duplicate vectors on retry (M-3)
- Proper Mock + isinstance-narrowed qdrant assertions in RAG isolation test (SEC-4 review)
- Atomic cooldown-check+claim and non-silent 2nd-claim failure (M-2)
- User-row-wins weights precedence mirroring load_profile (M-1)
- Correct cross-paper user-scoping docstring + two-tenant isolation test (SEC-4)
- Pass db_pool to source ctor so per-source rate-limiter persists (SEC-1)
- Narrow qdrant Filter.must types in data_purge assertions
- Real per-uid Qdrant vector counts in audit + correct test patch targets
- Declare lxml in jarvis-common group so cached_transport imports standalone (SEC-3 review)
- Real path-traversal test + SourceType enum in snapshot scope (SEC-2 review)
- Type-honest Rating enum in live review-sync test
- Partial-index ON CONFLICT predicate + live-PG fresh-insert regression
- Type-correct Qdrant-filter assertions in test_data_purge
- Harden cache-admission XML gate against entity-expansion DoS (SEC-3) + lock-in PI-D/PI-E tests
- Stamp X-Real-IP from Caddy + nginx real_ip_header X-Real-IP so rate-limit buckets per client
- Scope uploaded-PDF snapshots to user_library (SEC-2)
- Purge Qdrant vectors + audit-log on user hard-delete
- Webapp-configurable Langfuse dashboard URL
- Remove redundant header Discover CTA (4th entry point)
- Wire PaperTOC processingFailed to live failure signal
- Discover at top + header CTA + empty-state default + scope-honest facet/library copy
- Plain per-check remediation + record env-only-for-security-boot-gates principle
- Left pipeline list shows done/active/failed parity with the action sidebar
- Bounded search timeout so the palette fails fast instead of hanging
- Explain Lifecycle states + Flagged in plain language
- Facet counts honor the active library/corpus scope
- Configurable timeout + ReadTimeout retry + per-batch resume; GPU-resident via smaller dev chat models (no re-embed)
- Dedupe adds the canonical paper to the caller's library instead of a dead 409
- JobStatusResponse.user_id is int not str (runtime ResponseValidationError)
- Build pre-collapse schema for mig 046/047 tests instead of post-047 init.sql
- Trust init.sql schema_migrations bootstrap; run_migrations applies 073+ (incl 074) + live_pg connect-retry
- Pass native dict to JSONB params — codec was double-encoding (B5 regression)
- Teach unsafe-resolver guard CC-03 get_current_user_id + allowlist api-key-session
- Apply nginx api burst 20->40 (F-01) + accurate command-palette _reset docstring
- Single tooltip + capability-driven gates + presets + inline source config (F2)
- Accurate break-stop logging + no rehydrate flicker + phase-aware controls (F6)
- Live-services superset + Vector clarity + plain tooltips (F5)
- Per-filter subtitles + honest facet copy + discoverable upload (F4)
- Mobile-web-app-capable meta + nginx api_zone burst 20->40 (F8/Ops)
- Ruff import-sort (B2/B8 tests) + accurate clear-cooldown docstring
- Self-heal stuck rate-limit + honest dated diagnostic + UA/pacing/classify (B2)
- Journal GET date param str->date — fixes EOD 500 (B1, P1)
- Exclude credentials from source-cache key; strip hop-by-hop headers (PI-D/PI-E)
- 082 self-healing NULL-user reassign (no crash-loop) + 087 pulse_models user_id index (RB-D/DB-F04)
- Require_admin on extraction-template + topic CUD (PI-B/PI-C)
- PNG manifest icons + OfflineIndicator role=status + ResearchFeedPage exhaustive-deps (FE-E)
- Size api_zone for dashboard fan-out + widen trusted-proxy default + stop counting self-inflicted 503s in error badge (RB-A)
- Prod boot-gate enforces LITELLM_MASTER_KEY/POSTGRES_PASSWORD strength + APP_BASE_URL (SEC-A/SEC-B)
- Defense-in-depth user scoping on project-activity UNION + telegram project_detail (LE-OB4/TG-N2)
- Correct dead job-store invalidation keys so surfaces auto-refresh after batch jobs (FE-C)
- Unconditional SW API-cache purge on activate + controller-null logout race + clear review outbox on logout (FE-B/FE-A)
- Empty telegram secret on skip + first-run model-pull budget/banner + multi-mode login banner (RB-B/RB-C/INS-A)
- Scope promote_zotero_note to owning user (PI-A IDOR)
- Defer mig 086 in runner test (F1 blocker) + atomic dedupe-gated cards UPDATE in sync (F2)
- Drop compose empty-string LETSENCRYPT_* passthrough so Caddyfile defaults apply (Task E review fix)
- Fold deck_id into getNextReview, full Roman numerals, dedupe nav-group testid
- Restore admin health-pill quick per-service popover + keep full-report link (F4 regression)
- Resolve letsencrypt Caddyfile crash on empty LETSENCRYPT_* env vars
- Guard IDB persister attach when indexedDB absent (prod robustness) + fake-indexeddb in test setup (vitest exit 0)
- Vi.hoisted fixtures so PaperDetailPage.offline suite collects + runs (was 0/13)
- Replace require() with ESM import in ResearchFeedPage test (no-require-imports; closeout lint-clean)
- Drain zoteroGetLinkage once-queue in beforeEach — full vitest run deterministic green
- Make test fixtures type-correct; drop orphaned JournalSection test (tsc -b green)
- Replace Unicode smart-quotes with ASCII in EndOfDaySection JSX expression (tsc -b TS1127)
- Repair f1 test vi.mock hoisting + drop dead Ask branch
- Restore paper lifecycle actions in 3-pane right rail; rm orphaned SummaryTab; scroll-spy anchor; lint
- Wire UI_v3 mocked e2e specs into test:e2e:mocked; restore design mockups; drop orphaned ui-store.heroMode
- Live counts on §MILESTONES/§TASKS headers; untrack provisional projects.jpg
- Real IngestionSection filterGroups split — §VI shows only Spaced Repetition (Conflict-5)
- EOD first-move hint reflects journal-only persistence (no false forward-seed promise)
- Drop __future__ annotations breaking account OpenAPI; defer migrations 083-085 to runtime runner
- Reject pending_email (email-change) tokens at /auth/verify — close passwordless-login bypass; +type fixes
- Anchor _compute_streak on UTC date to match executive.py exactly
- Type-cast facet row fields so TopicFacetCount construction is sound (pyright)
- Widen _assert_project_owner conn type to PoolConnectionProxy (pyright)
- Cache header-type consistency + single-user Continue stays clickable


### Documentation
- MkDocs-Material operator/developer docs site → GitHub Pages
- Refresh deferred backlog post CI-green program (Hermes, Performance&hardware-fit, 046/047-residual, installer/docs-site, Qdrant-re-embed-conditional)
- Correct stale mig-046 test comment
- Mark shipped --no-deps / discovery-reliability items DONE
- Fix 10 verified drift items (migration count, deprecated env, broken/stale refs, CHANGELOG regen) + archive superseded audits
- Add end-user guide (surfaces + plain-English sign-in/recovery), index in docs/README
- Canonical post-UI_v3 follow-ups execution plan
- De-link removed PomodoroTimer.tsx in 2026-05-02 decisions doc (UI_v3 deleted it; fixes check_agent_docs)
- Land 8 IA redesign specs + INDEX + parallelized execution plan
- Add companion docs site + complete user guide (Planned; UI-guide gated on redesign)
- Setup.sh --check + single/multi mode + source HTTP cache env vars
- Add public ROADMAP.md (shipped v0.4.1 / in-progress / planned Hermes+offline)
- Correct stale carried follow-ups (resolve_owner_chat_id NOT dead; py-spy/feed-500 closed in v0.4.1) + log v0.4.1-surfaced opens


### Features
- Promote Discover to top-level + rename Research Feed→Library
- Wire global Cmd-K command palette to searchPreview (F1)
- PATCH source config + clear-cooldown admin endpoints (B5)
- GET /api/setup/smtp (masked) + smtp.* in settings allow-list (B4)
- GET /api/system/capabilities (networkx/sklearn probe) (B6)
- Shared client stubs + types + lazy routes + cmdk
- POST /api/review/sync idempotent offline replay + mig 086 + offline-tolerant session grace (+LE-OB5)
- Send sign-in link + request-link enumeration-timing hardening (+A-3)
- System Health per-check explanations + dev-mode context banner
- Shared client additions — admin sendSignInLink + getNextReview deckId (gate for B/F)
- Offline flashcard review outbox + idempotent sync-on-reconnect (client) + endpoint contract handoff
- Connectivity banner + per-surface offline indicators (Library/Paper Detail) + install affordance
- Offline last-known-good route-guard (online path unchanged) + logout purges persisted query cache
- Persist read-surface query cache to IndexedDB (last-known-good); NON-GOAL queries excluded; logout-purge API
- PWA foundation — manifest, service worker (read-surface SWR cache, NON-GOALs network-only), online-status hook
- Settings IA redesign — 2-pane §-rail + §I Account section
- Feed faceted 3-pane IA — §-facet rail + scoped filter + Inbox-first default
- Learning Cards IA redesign — focused review session shell
- Analytics IA redesign — Reflect hero + KPI band + section renames
- F2 — 3-pane research-log IA redesign
- F3 — My-Day parity, Pomodoro §1a/§1c/§1d, EOD shutdown ritual
- Projects IA redesign — chapter rail + scrolling document pane
- Shell/sidebar grouped roman-numeral nav + Ask page + admin breadcrumbs
- F0 shared scaffolding — api client fns + types + routes for 8 surfaces
- Self-service GET/PATCH /api/account with verified email change
- Add GET /api/analytics/summary period-delta endpoint (B5)
- Thread entity + on-the-fly Yesterday rollup + EOD shutdown-ritual persistence
- Project_questions table + CRUD + recent-activity UNION
- Add UI v3 facet counts — by_source, by_topic, untagged
- Mode-adaptive SMTP step in first-run wizard (logic-only)
- Setup.sh --check doctor + --mode single|multi + OS-aware remediation
- CoreSettings.jarvis_setup_mode + setup_mode in /api/setup/status


### Miscellaneous Tasks
- Land CI-green + verified-gap-closure plan
- Remove unused data-popover-testid from HealthDots trigger (Task C review nit)
- Remove leftover /tmp/zotero-diag.log debug instrumentation from ResearchFeedPage test
- Remove dead JournalSection component + stale tracked test (replaced by EOD redesign)


### Performance
- My-Day bundle + Analytics staleTime + HeaderPill poll gating (F7)
- Mig 088 indexes + executive /my-day gather+SQL-streak + /my-day-bundle (B7)
- Env-tunable stage2 timeout (900) + LLM concurrency (4) (B8)
- In-memory GET cache for external metadata hosts (httpx-cache, lean/no-dep)


### Refactoring
- Explicit column allowlist in update_thread mirroring notes (M-4)
- NAMESPACE_DNS named constant + honest retry-determinism test (M-3 review)
- Drop dead deck_id param getNextReview sent to a backend that ignores it
- Get_current_user_id Depends — search router (CC-03, behavior-preserving)
- Get_current_user_id Depends — recommendations router (CC-03, behavior-preserving)
- Get_current_user_id Depends — recommendation_feedback router (CC-03, behavior-preserving)
- Get_current_user_id Depends — rag router (CC-03, behavior-preserving)
- Get_current_user_id Depends — pulse router (CC-03, behavior-preserving)
- Get_current_user_id Depends — papers router (CC-03, behavior-preserving)
- Get_current_user_id Depends — notes router (CC-03, behavior-preserving)
- Get_current_user_id Depends — feed router (CC-03, behavior-preserving)
- Get_current_user_id Depends — shared infra + test harness (CC-03, behavior-preserving)


### Testing
- Structural init.sql↔migration-sequence drift guard
- Cover run_process_pdf EmbeddingBatchError resume path
- --check side-effect-free + mode→.env + mode-adaptive SMTP step


### Harden
- Never buffer binary/PDF bodies (structural metadata-only guard)


### Style
- Isort test_pulse_stale_fallback to project ruff norm (CC-03 follow-up)


### Major Programs (2026-05-23 → 2026-05-24)

- **Deep-Audit cycles 1+2+3** (2026-05-23) — 82 findings closed across 3 fix passes. 8 cross-user leak fixes, 5 admin gating gaps closed, 3 XSS/CSP fixes (SEC-XSS-001/002 + SEC-CSP-001), 14 transformers CVEs, FastAPI/Starlette 0.126.0/0.50.0 (closes CVE-2025-62727), Pulse correctness bugs, `contradiction_jobs` user_id propagation.
- **Bloat-Reduction program** (2026-05-24) — 5 god components decomposed. 260 inline `queryKey:` migrations. 12 telegram test migrations. 55 jarvis_common docstrings. Net +1963 LOC structural.
- **Dead-Code Purge program** (2026-05-24) — 7 orphan frontend hook/util files removed (−213 LOC). 7 B-list rot-on-touch follow-ups closed. 5-partition dead-code inventory generated.
- **Polish Wave** (2026-05-24) — 2 final rot-on-touch follow-ups. Vulture tooling removed (zero-yield, wrong fit for decorator-heavy Python; `knip` retained for frontend). 4 pre-existing failures fixed (SettingsAIPanel TS2532, chat-confidence vitest, 11 auto-fixable lint warnings, mkdocs install advisory). Version metadata + CHANGELOG + v0.5.0 git tag. **`libs/jarvis_common/jarvis_common/testing.py` decomposed** (945 LOC → 5 submodules + thin facade).

## Pre-public development (v0.1 – v0.4.1)

The v0.1 through v0.4.1 releases represent the full private development phase. The core RAG pipeline was built across this period: multi-source paper discovery (arXiv, Semantic Scholar, OpenAlex, PubMed), PDF extraction with page-level citation provenance, a three-stage LLM-reranked Pulse recommendation engine, and a semantic knowledge graph with entity extraction and contradiction detection. Spaced-repetition learning cards (FSRS) and a daily executive-function interface (My Day, Pomodoro timer, journal, project tracking) were added alongside the recommendation system. Multi-tenancy and security hardening — magic-link authentication, strict user_id scoping across all data paths, per-user FSRS and recommendation state, cross-user isolation CI gates, Docker Secrets, and a container-hardening sweep — were progressively applied from v0.2 onward. The job infrastructure was migrated from a custom worker to procrastinate-backed async task queues with SSE progress streaming. Observability tooling (Langfuse, Sentry, structured audit logging) and a one-shot installer wizard were added in v0.3–v0.4. The v0.4.1 release closed the last known cross-tenant data leaks and completed a full adversarial-review pass before the v0.5.0 pre-release consolidation.

<!-- generated by git-cliff -->
