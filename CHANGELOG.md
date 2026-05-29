# Changelog

All notable changes to JARVIS RD Assistant are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).
## [unreleased]

> An internal `v0.5.0` git tag was cut locally on 2026-05-24 for version-metadata alignment with `pyproject.toml` / `frontend/package.json`. The repo remains private; this release line stays semantically **unreleased** until a public release event. When that happens, this `[unreleased]` heading will be retitled to `[v0.5.0] - <release date>` (matching the existing `[vX.Y.Z]` style).

### Dependency security & PDF extraction (2026-05-29)

The PDF text-extraction engine was migrated from marker-pdf to **Docling**. This closed the last dependency CVEs that marker-pdf's transitive version caps had been blocking — Pillow (6 advisories), the `diskcache` transitive (dropped via instructor 1.15), and a transformers code-execution advisory (fixed in 5.x) — and moved openai to the 2.x line. It also made citation **page numbers exact**: chunks are now bounded to a single page using Docling's per-page provenance, so a cited page always matches the page snapshot the reader sees.

### Post-public-readiness audit (2026-05-26)

A six-week internal audit-and-remediation pass closed roughly 120 findings ahead of the first public release. The themes below capture user-visible and operator-visible changes; commit-level detail follows in the per-area sections.

**Security.** The background-job Server-Sent Events stream now requires an authenticated session — it previously accepted unauthenticated subscriptions and returned job state for the NULL user. All cross-user data paths were re-audited: project recommendations, paper-source feedback, author alerts, and review-deck queries are now scoped to the logged-in user, and admins cannot read other users' research data. Prompt-injection vectors in PDF body text, paper titles, discovery snippets, and tracked-author bios are stripped before reaching the model with a documented prompt-shape contract enforced by an AST check. Container processes drop privileges, run with `no-new-privileges` set, and ship with a root-level `.dockerignore` so secret files and host-bound paths cannot accidentally land in the build context. Lock-file integrity (Python `uv.lock`, npm `package-lock.json`) is now verified against registry pins at install. Append-only audit logs reject `UPDATE`/`DELETE` at the PostgreSQL rule level, and the pairing-code length, rating regex, and ProjectManager method signatures were tightened against malformed input.

**Correctness.** Several long-standing cross-tenant bugs were fixed: the recommender's project query now filters by `user_id`; author-alert dedupe is per-user instead of global; the Zotero push flow no longer leaks `paper_id` across sessions. A handful of API surfaces that previously returned 200 with inconsistent envelopes now return one shape, and the streaming Chat error path surfaces transport errors to the UI instead of swallowing them. The `paper_sources` table and `PaperSource` abstract base were brought into symmetry so the catalog the UI shows matches what the ingestion job actually runs.

**Architecture.** Two oversized modules were split by responsibility: `entities.py` (814 LOC) became a typed router + a Postgres adapter + a Qdrant adapter; `routers/settings.py` was decomposed by settings domain. A new internal Telegram bot API removes the previous Telegram-bot → paper-ingestion DB-coupling. The migration history was squashed: the 88-file pre-v0.5.0 chain became a single `db/init.sql` bedrock with new migrations starting at `0089`, and `tests/test_baseline_invariants.py` pins the schema invariants.

**Developer experience.** Continuous integration now enforces type-check (Pyright zero errors), a test-shape contract (each test belongs to one of four documented shapes), the LLM prompt-shape AST check, and PII / burned-secret allowlists. The CI workflow was migrated to `astral-sh/setup-uv@v6` with a Python 3.12 pin and `uv sync --frozen`, cutting wall-clock from 8–15 minutes to 4–5 minutes. A pre-commit hook runs the same gates locally.

**Public-launch preparation.** This release ships a rewritten README with above-the-fold product screenshots, a Highlights section, and the four-audience deployment path; weekly `dependabot` updates for pip, npm, Docker base images, and GitHub Actions; structured GitHub issue templates (bug report, feature request) with security reports routed to a private GitHub Security Advisory; and a root `SECURITY.md` pointing to the threat model.

### Pristine pass (2026-05-25)

Pre-public-launch pristine pass landed at `cadf8305`. 53 commits across 9 waves + 3 sub-waves.

- **W1–W6**: closed audit-remediation carry-forwards (CI green unstick, 28 trivial CFs, 3 FE CFs, 14 backend CFs incl. HIGH/MED), shipped TELEGRAM-INTERNAL-API-1 + INFRA-INGEST-1, and split ARCH-ENTITIES-1 (entities.py 814→329 LOC + entities_qdrant.py + entities_sql.py).
- **W6.5** (pre-pristine CF burn-down, YAGNI lens): 10 actionable CF fixes (`469bb978`..`55bfe9ef`) + wave6.5-fix (`e2466c02`) addressing Wave-Gate Axis 2/4/5 findings + 5 DEFERRED-INTENTIONAL downgrades; ARCH-AUTH-1 closed in `docs/known-residual-risks.md` (YAGNI, no coupling defect).
- **W7** (OBS-1 RESOLVED, `e7d8687a`): `git log origin/master --all --full-history --diff-filter=ACMRT -- secrets/langfuse_init_pk.txt secrets/langfuse_init_sk.txt` returns ZERO commits; verified no `git filter-repo` / force-push required for public-launch.
- **W8** (push + CI): pristine/main fast-forward-pushed to `origin/master` after all local pristine gates green (ruff / pyright / check-test-shape / check_agent_docs / check-burned-secrets / check-python-deps / pytest 2647/0/1-skip / FE lint+test+build / mkdocs --strict).
- **W8.5** (baseline failure fixes): pulse `degraded_reason` clobber root-cause (`069e798c` + `46f299ce` refinement: pass `stats[degraded_reason]` to persist not the clobbered local var); reauth rate-limit-cache cross-file `_TEST_CHAT_ID` collision (`595ed887`: 99999 → 55555); 2 jsonb-double-encode guard violations in hw_probe + settings_ai (`36befbde`: drop `json.dumps()` wrappers before `$N::jsonb`).
- **W8.6** (CI flake + speed-up): protocol-level `psql` round-trip probe in `_spin_pg_container` (`be2b4c1e`) supplements the W6-01 TCP probe to close the handshake-race window; zombie-container pre-clean + exit-125 retry in `_spin_pg_container` (`f1f12d80`) handles CI-retry zombie collisions; CI workflow upgraded to `astral-sh/setup-uv@v6.8.0` + `python-version: "3.12"` pin + `uv sync --frozen` (`466c6841`), cutting CI wall-clock from ~8-15min to ~4-5min.
- **Post-push CI**: 3 consecutive green runs on master (`36befbde` post CI-E retry, `e52591f7` attempt 1 first-try, `cadf8305` first-try). Both flake fixes (psql probe + docker-125 retry) validated by live CI.

### Upgrade Notes

- **Migration baseline squashed.** The 88-file migration chain prior to v0.5.0 was consolidated into `db/init.sql` as the single baseline; new migrations start at 0089. The migration runner detects squashed-init state and applies forward without interruption — operators upgrading from v0.4.1 or earlier need no manual intervention. See `tests/test_baseline_invariants.py` for the schema invariants pinned.

### Security
- Cross-tenant project leak in recommender (`SEC-XTENANT-1`): `_refresh_recommendations_for_user` now scopes the projects query to `user_id`. Audit-remediation wave W1-T1.
- Append-only `audit_log` (`SEC-AUDIT-1` / migration `0090_audit_log_append_only.sql`): blocks `DELETE`/`UPDATE` on the audit table via PG rule. W2-T9.
- Per-user author-alert dedupe (`SEC-AUTHALERT-1` / migration `0091_author_alert_log_user_dedupe.sql`): `ON CONFLICT (user_id, tracked_author_id, paper_id)` prevents cross-user alert suppression. W2-T13.
- Owner-override guard tests + audit-log emission (`SEC-OWNER-1` / `SEC-OWNER-2`). W2-T7, W2-T8.
- Pairing-code length bound (`SEC-RC-1`), rate-card regex (`SEC-RATING-1`), mandatory `user_id` on 3 ProjectManager methods (`SEC-PRJMGR-1`). W2-T10..T12.

### Bug Fixes (audit-remediation wave)
- Email verification flag now respects SMTP exception path (`BUG-EMAIL-1`). W1-T2.
- Zotero BYTEA decode uses `crypto.resolve_secret_row` (memoryview-safe) (`BUG-ZOTERO-1`). W1-T3.
- Summarizer HTTPException propagation guarded at `paper_jobs.py:231, 270` (`BUG-SUMMARIZER-1`). W1-T4.
- Unread guard in `feed_query.py` resolves contradictory WHERE composition (`BUG-FEED-1`). W2-T14.
- `vector_writer` role boot-time password drop guard (`BUG-DBINIT-1`). W2-T15.
- Three missing CI smoke secrets (`BUG-CISMOKE-1`). W2-T16.
- `arxiv_source.py` parses `response.content` (bytes) instead of re-encoding `response.text` (`CFG-XML-1`). W3-T10.
- `weekly_summary.py` ThemeOutput stays as Pydantic instance, no dict.get on LLM output (`CFG-LLMOUT-1`). W3-T13.
- `pulse/job.py` degraded_reason OR-chain preserves earlier value (FALSIFIED CI-B per Phase A; no change needed).
- 13 additional `CFG-*` MEDIUM fixes (config validation, GDPR scoping, dynamic model field-name validation, CIDR cache, etc.). W3-T1..T18.
- 9 FRONTEND error-sentinel + per-tab error handling fixes (`FE-IDLE-1`, `FE-TRIAGE-1`, `FE-CHAP-1`, `FE-MD-1`, `FE-CP-1`, `FE-REVMODE-1`, `FE-DM-1`, `FE-API-1`, `FE-SSE-1`). W4-T1..T9.
- 24 cross-cutting fixes: `decompose_query` doc catalog, sentry-init helper, fixture deduplication, `LockNotAvailableError` simplification, jobs throttle elapsed-seconds, `faux_qdrant` dim-mismatch + null-field guards, email format → replace, `_retry_after_seconds` cap at 3600, `_HAS_QWEN3` guard, `ScoredCandidate` frozen, scheduled `magic_link_tokens` purge, `SourceType.ZOTERO` enum, init-secrets.sh dedupe, profile.sh portable compose ps, TS-08 carve-out registry enforcement (`F-*`, `BE-*`). W5-T1..T24.

### Hardening
- CI-CROSS-USER-FLAKY-1 mitigation: `_spin_pg_container` adds post-`pg_isready` TCP socket probe (30s deadline + 250ms retries) to eliminate SSL-init race that produced `ConnectionResetError [Errno 104]` on GitHub Actions runners. W6-01.

### Deferred / Documented
- `## TELEGRAM-INTERNAL-API-1`, `## ARCH-AUTH-1`, `## ARCH-ENTITIES-1`, `## INFRA-INGEST-1`, `## CI-CROSS-USER-FLAKY-1` added to `docs/known-residual-risks.md` with reopen criteria.
- `OBS-1-RESIDUAL` git-history secret-purge remains operator-deferred (runbook at `docs/SECURITY.md:165-210`; repo private).

### Bug Fixes
- Narrow APIRoute for route-path assertions (RB-3 follow-up)
- Owner-override resolver on Telegram-reachable LE endpoints + accurate cross-service boundary doc (RB-3)
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
- Narrow qdrant Filter.must types in data_purge assertions (RB-2 review follow-up)
- Real per-uid Qdrant vector counts in audit + correct test patch targets (RB-2 review)
- Declare lxml in jarvis-common group so cached_transport imports standalone (SEC-3 review)
- Real path-traversal test + SourceType enum in snapshot scope (SEC-2 review)
- Type-honest Rating enum in live review-sync test (RB-1 follow-up)
- Partial-index ON CONFLICT predicate + live-PG fresh-insert regression (RB-1)
- Type-correct Qdrant-filter assertions in test_data_purge (RB-2 follow-up)
- Harden cache-admission XML gate against entity-expansion DoS (SEC-3) + lock-in PI-D/PI-E tests
- Stamp X-Real-IP from Caddy + nginx real_ip_header X-Real-IP so rate-limit buckets per client (RB-4)
- Scope uploaded-PDF snapshots to user_library (SEC-2)
- Purge Qdrant vectors + audit-log on user hard-delete (RB-2)
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
- Make Wave-2 test fixtures type-correct; drop orphaned JournalSection test (tsc -b green)
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
- MkDocs-Material operator/developer docs site → GitHub Pages (WS2, hybrid, no file moves)
- Refresh deferred backlog post CI-green program (Hermes, Performance&hardware-fit, 046/047-residual, installer/docs-site, Qdrant-re-embed-conditional)
- Correct stale mig-046 test comment
- Mark shipped --no-deps / discovery-reliability items DONE
- Pristine-hardening program plan + QA/UX/perf deep audit
- Fix 10 verified drift items (migration count, deprecated env, broken/stale refs, CHANGELOG regen) + archive superseded audits
- Add end-user guide (surfaces + plain-English sign-in/recovery), index in docs/README
- Canonical post-UI_v3 follow-ups execution plan (deep-plan output)
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
- Shared client stubs + types + lazy routes + cmdk (Wave-0 Task S)
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
- Land CI-green + verified-gap-closure plan (deep-execute step 0)
- Remove unused data-popover-testid from HealthDots trigger (Task C review nit)
- Remove leftover /tmp/zotero-diag.log debug instrumentation from ResearchFeedPage test
- Remove dead JournalSection component + stale tracked test (replaced by EOD redesign)


### Performance
- My-Day bundle + Analytics staleTime + HeaderPill poll gating (F7)
- Mig 088 indexes + executive /my-day gather+SQL-streak + /my-day-bundle (B7)
- Env-tunable stage2 timeout (900) + LLM concurrency (4) (B8)
- In-memory GET cache for external metadata hosts (Bucket-H httpx-cache reopened, lean/no-dep)


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
- Structural init.sql↔migration-sequence drift guard (WS3 / 046-047 class)
- Cover run_process_pdf EmbeddingBatchError resume path
- --check side-effect-free + mode→.env + mode-adaptive SMTP step


### Harden
- Never buffer binary/PDF bodies (structural metadata-only guard)


### Style
- Isort test_pulse_stale_fallback to project ruff norm (CC-03 follow-up)


### Major Programs (2026-05-23 → 2026-05-24)

- **Deep-Audit cycles 1+2+3** (2026-05-23) — 82 findings closed across 3 fix-waves. 8 cross-user leak fixes (W1-D1/D2), 5 admin gating gaps closed (W2-S1), 3 XSS/CSP fixes (SEC-XSS-001/002 + SEC-CSP-001), 14 transformers CVEs, FastAPI/Starlette 0.126.0/0.50.0 (closes CVE-2025-62727), Pulse correctness bugs, `contradiction_jobs` user_id propagation.
- **Bloat-Reduction program** (2026-05-24) — 5 god components decomposed. 260 inline `queryKey:` migrations. 12 telegram test migrations. 55 jarvis_common docstrings. Net +1963 LOC structural.
- **Dead-Code Purge program** (2026-05-24) — 7 orphan frontend hook/util files removed (−213 LOC). 7 B-list rot-on-touch carry-forwards closed. 5-partition dead-code inventory generated.
- **Polish Wave** (2026-05-24) — 2 final rot-on-touch carry-forwards (CF-W0G1, CF-W0G2). Vulture tooling removed (zero-yield, wrong fit for decorator-heavy Python; `knip` retained for frontend). 4 pre-existing failures fixed (SettingsAIPanel TS2532, chat-confidence vitest, 11 auto-fixable lint warnings, mkdocs install advisory). Version metadata + CHANGELOG + v0.5.0 git tag. **`libs/jarvis_common/jarvis_common/testing.py` decomposed** (945 LOC → 5 submodules + thin facade).
## [v0.4.1] - 2026-05-15


### Bug Fixes
- Scope entity_relationships reads to caller-visible papers (close cross-user leak)
- Inline defaults for new compose interpolations so config renders env-free (INF-hardening followup)
- Bound CardCreate text, rate-limit /api/me/export + telegram whoami (Wave3 DoS)
- Scope intent_repo/project counts/my-day + unlink TOCTOU + drop redundant json reload (Wave3 LE)
- Atomic source-slot claim so concurrent workers can't double-fire (H-1)
- Readiness + litellm entrypoint reject weak LITELLM/POSTGRES secrets (H-5)
- Accept valid session as sufficient for verify_api_key global gate (WS-AUTH-KEY-SESSION followup)
- Add lastError to job-store.test AuthState mock (WS-AUTH followup)
- Jarvis-setup.sh must generate secrets before compose up (H-4 followup)
- Correct _make_context return annotation (RB-5 followup)
- Scope check_tracked_authors recent_papers to user_library (RB-2 followup)
- Rewrite Host so nginx default_server stops 444ing dashboard (H-6)
- Never log magic-link token; require SMTP in prod when not log-only (H-2)
- Generate all required secret files incl infra_ingest_key (H-4)
- Scope BM25/fallback list_papers to user_library (RB-1)
- Provision PYTHONPATH so cross-user isolation gate actually runs (CI-1)
- Require session + scope all 6 endpoints to user_id (RB-2)
- Convert get_event_loop tests to async for Py3.12+uvloop (CI-3)
- Add session auth + scope GET to caller library (RB-3)
- Use fresh auth_check user_id in review handlers (RB-5)
- .npmrc legacy-peer-deps so npm ci tolerates react19 peers (CI-2)
- Reject create_card into another user's deck (RB-4)
- Preserve outer X-Forwarded-Proto instead of clobbering with $scheme


### Documentation
- Pin assert_paper_ownership canonical-corpus invariant + regression test (D4 escalate)
- Fix CHANGELOG links, reconcile DEPLOYMENT/residual-risks to multi-tenant GA, archive old audits (D-2,3,7,8,10,11)
- Reconcile PRD/REQUIREMENTS/ARCHITECTURE to HEAD + multi-tenant GA (D-1,4,5,6,9)


### Features
- POST /api/auth/api-key-session mints scoped owner session (WS-AUTH-KEY-SESSION)


### Miscellaneous Tasks
- Import-org + kg-test type:ignore (D4 followup cleanup)
- Defence-in-depth — no-new-privileges, vector entrypoint, CI checks, SHA-pin, nginx real_ip (INF-hardening)
- Gate py-spy behind INSTALL_PROFILING build arg; prod excludes it
- Remove redundant check-no-jsonb-double-encode.sh; .py is canonical (DB-04)


### Refactoring
- Remove assert_paper_ownership multitenant knob — shared canonical corpus is decided + SECURITY.md boundary (D4 resolved)
- Fix job-store feed key, consolidate _owner_headers, drop dead _probe_postgres, relocate useFeedCounts (Wave3 DRY)


### Testing
- Align magic-link event + prod-config tests with H-2 security behavior
- Regression — /api/papers/feed 200 across limits (BATCH-A)
## [v0.4.0] - 2026-05-15


### Bug Fixes
- Isolate get_secrets_settings/api-key caches in paper_ingestion conftest
- Make db/init.sql self-consistent — fresh-volume Docker installs were broken
- CODEOWNERS handle @ferhatfidan -> @FFidan (actual repo owner per remote)
- Make $$ state machine comment-aware (mig 080 FOREACH crash)
- Hash email in stdlib auth logs + clear pre-existing ruff debt + COC/SECURITY contact
- B1 — audit_log column is timestamp not created_at


### Documentation
- WS-MULTITENANT-DOCS — SECURITY threat model, DEPLOYMENT retraction, README quickstart, CONTRIBUTING, CODE_OF_CONDUCT


### Features
- WS-PRE-PUBLIC-CHECKLIST — readiness endpoint, System Health page, Sentry opt-in
- Audit-log + per-user rate-limit + user-deletion cascade + installer


### Testing
- WS-NEGATIVE-TESTS — cross-user isolation suite + self-enforcing CI gate
## [v0.3.5-rc] - 2026-05-15


### Bug Fixes
- Purge conflict-marker residue from 6 non-scope files
- Mig-077 test expects papers.discovered_by (mig 072 rename)
- Adversarial-review blockers — B1/B2/B3/B4 + N1-N5 + test debt
- Enforce per-user isolation in paper_ingestion
- Detect aliased + attribute-form Depends in unsafe-resolver linter
- Exempt /api/setup/* from verify_api_key middleware
- Stop logging users out on 403 + add 401 toast


### Features
- WS-CROSS-USER learning_engine — replace permissive resolvers with strict auth
- Clear React-Query cache + zustand stores on logout (WS-FRONTEND-HYGIENE)
- WS-DEV-MODE-RETIREMENT — granular dev flags + meta-flag promotion
- Forward X-Owner-User-Id on all user-data backend calls (WS-CROSS-USER)
- WS-AUTH foundation — demote API key to ops-only
## [v0.3.4] - 2026-05-14


### Bug Fixes
- Mig 077 — papers.user_id was renamed to discovered_by in mig 072
- Tsc-strict compat for PulseDeck + JournalSection tests
## [v0.3.3] - 2026-05-14


### Bug Fixes
- JournalSection preserves typed text on refetch (N3)
- Paper_digest per-pairing X-Owner-User-Id (N1)
- Tighten CORS wildcard check to membership (L-03)
- JournalSection useQuery + PulseDeck per-card savePending (M-12, M-13)
- _handle_pairing uses @rate_limit decorator (M-05)
- Reset outage flags before recovery INSERT (L-06)
- AdminUsersPage setState into mutation lifecycle (H2)
- Refresh warn_multitenant_stub log message (M-08); vault narrative (H1)


### Documentation
- Refresh contracts §6.2 and §10 — call_llm_structured (L-11)
- 2026-05-14 deep-audit refresh (post-Wave-0)


### Features
- Scope KG read endpoints by user_id (M-01..M-04)
- Admin-gate /api/nudges + FSRS per-user config + null guard + tx (L-12, L-13, L-14, M-11)
- Rate-limit /setup/status and /auth/logout (L-01, L-02)
- AST-based JSONB double-encode linter (M-10)
- Migration 079 — JSONB repair for audit_log + job_progress (M-09)
- Migration 077 — FK constraints on 18 user_id tables (H4)
- Structlog PII key scrubber (L-07)
- Asyncio.wait_for timeout per probe (L-08)
- Require JARVIS_MODEL_HMAC_KEY in production (M-07)
- Thread user_id through extract_entities_for_paper + batch (H3)
- Migration 078 — paper_entities.user_id + backfill (H3)


### Miscellaneous Tasks
- Suppress I001 in test_pulse_training.py via per-file-ignores
- Refresh M-05/M-08 tests + ruff I001
- Add resolve_secret_row to __all__ (L-05)


### Refactoring
- Extract make_postgres_probe / make_litellm_probe (L-10)
- Drop l2_penalty signal — feature already in embedding (M-06)
## [v0.3.2] - 2026-05-14


### Bug Fixes
- Scope sibling handlers + orchestration by user_id (Wave 0.4 sweep)
- User_id-scope complete_task + create_project
- Scope /briefing /tasks /done /projects /newproject by user_id
- Auth_check returns user_id; auth_required stashes on context


### Refactoring
- Residual 4.2+4.3 changes (post-edit hook sweep)
## [v0.3.1] - 2026-05-14


### Bug Fixes
- FeedView onHardDelete stable callback + AdminUsers per-row delete + nits (Wave 4-7 closeout)
- Per-user scoping for liked CTE + weight config + l2_lambda out of weights (3.1-3.3)
- Disable PulseCard Save during pending mutation (DOM-F-03)
- Boot-gate JARVIS_CONFIG_KEY in production (DOM-E-08)
- Close HTML tags at chunk boundaries in _send_chunked (DOM-D-09)
- Reorder ownership-vs-null guard + form-field max_length (3.7, 3.8)
- Enable allow_credentials for cross-origin session cookies (DOM-E-12)
- Tighten assert_paper_ownership NULL semantics in multi-tenant (DOM-E-04)
- Allowlist-check S2 openAccessPdf.url at parse time (DOM-B-09)
- Explicit X-API-Key on daily_briefing stats call (DOM-D-04)


### Documentation
- Explain Optional noqa edge case in dynamic_models (Task 7.4)


### Miscellaneous Tasks
- Add exc_info to bare except Exception blocks (Task 7.1)
- Profile-gate vector service to avoid unintended docker-socket access (DOM-H-03)


### Performance
- Memoize FeedPaperRow + lazy-split ResearchFeedPage (6.1, 6.2)
- Per-row pendingRoleUserId on AdminUsersPage (DOM-F-07)
- Wrap detect_hardware in asyncio.to_thread at 4 async call sites (DOM-J-07)


### Refactoring
- Extract warn_multitenant_stub + health-check skeleton (4.2, 4.3)
- Consolidate _coerce_bool, sse shim, decompose_query param, dead models, predicate inlines (4.4-4.8)
- Switch config from @dataclass to BaseSettings (Task 7.3)


### Testing
- Update _make_profile to use l2_lambda field after 3.3 refactor
- Cover shared init_langfuse_hook (DOM-J-01)
## [v0.3.0] - 2026-05-14


### Bug Fixes
- Defer migrations 075/076 + drop future-annotations to unblock OpenAPI schema (Wave 2 closeout)
- Per-endpoint rate limits on setup + /pair (H16, DOM-D-02)
- Require paper ownership on compute_paper_priority (Wave 1 closeout)
- Require paper ownership on extract_entities (Wave 1 closeout)
- Per-endpoint rate limits on request_link + verify (H15)
- Drop double json.dumps in SystemEventHandler + widen lint window (Wave 1 closeout)
- Validate deck ownership in single-paper generate_cards (Wave 1 closeout)
- Drop public-literal HMAC fallback + dedicated key (H14)
- Mig 075 backfills double-encoded rows (Wave 1 closeout)
- Wrap 5 lazy routes in Suspense (H12)
- Drop double json.dumps + add nolint markers (Wave 1 closeout)
- Use hmac.compare_digest for INFRA_INGEST_KEY check (H13)
- Wire telegram_user_pairings into auth_check (C1)
- Sanitize bulk_action error responses (DOM-A-07)
- Scope to author + assert paper ownership (DOM-A-05/06/14, DOM-C-07)
- Block personal-key NULL fallback for non-admin readers (DOM-A-09)
- Hoist assert_paper_ownership above async_mode branch (H9)
- Require paper ownership on graph + fetch + get endpoints (H11)
- Scope analytics handlers to caller + add llm_usage_log.user_id (H8)
- Scope batch_process_papers via user_library JOIN (H10)
- Scope decks + cards.create_card to caller (H5, DOM-C-06)
- Thread user_id through generate_cards_core (H7)
- Drop double json.dumps in my_day.upsert_journal_entry (H2)
- Scope anki export to deck owner (H6)
- Drop double json.dumps in _handle_pairing (H4)
- Drop double json.dumps in _autoconfigure_models_hook (H4)
- Drop double json.dumps in setup._persist_config (H3)
- Drop double json.dumps in infra_events.bulk_ingest (H1)
- Merge no-user FROM into corpus-user variant to type $1


### Documentation
- 2026-05-14 deep audit + security review baseline


### Miscellaneous Tasks
- Rephrase JSONB test docstring to avoid lint false-positive
- Add check-no-jsonb-double-encode gate (Wave 1)
- Install py-spy in paper_ingestion image for profiling
- Restrict vector docker_logs to this compose project
- Align pydantic-ai-eval deps with eval doc recommendation
- Use --no-deps in profile-stack-up to avoid sibling-Ollama collision
## [v0.2.2] - 2026-05-14


### Documentation
- Close §6 §9 §10 §11 §P0.3 via 2026-05-14 cleanup sprint
- Baseline report + ranked follow-ups (closes §11)
- Archive stale multiuser plans per deletion manifest
- Add 2026-05-14 post-v0.2.0 cleanup sprint plan
- Close items via 2026-05-14 functional sweep
- Deletion-candidate manifest for pre-v0.2.0 plans


### Features
- User-scope Qdrant search + backfill script
- Per-user topic subscriptions wire fan-out


### Miscellaneous Tasks
- Pyright fixes + migration 074 deferred-set + plan doc


### Performance
- Cache get_secrets_settings() in build_litellm_headers hot path
- Split OnboardingTour (react-joyride) out of eager bundle


### Refactoring
- Typed SecretsSettings replaces read_secret helper
- Fail-loud when no pairings; delete single_tenant_user_id


### Testing
- Move secrets-cache clear fixture to root conftest
- Assert BM25 leg of hybrid_search remains user_id-unscoped (Decision 6)


### Spike
- Pydantic-ai evaluation + recommendation doc for §13
## [v0.2.1] - 2026-05-12


### Bug Fixes
- Scope config and harden job ownership
- Add --check to ruff-format (Bucket E3 mitigation §M1)


### Documentation
- Post-v0.2.0 roadmap — deferred + new follow-ups


### Miscellaneous Tasks
- Close deferred roadmap tranche
## [v0.2.0] - 2026-05-10


### Documentation
- 2026-05-10 baseline numbers + negative results (Bucket G)


### Features
- Typed Settings classes for jarvis_common + paper_ingestion + learning_engine


### Miscellaneous Tasks
- Add pydantic-settings + structlog to jarvis-common dep group
- Make profile target + scripts/profile.sh + HOWTO (Bucket G)


### Performance
- React.lazy 5 heavy pages — main bundle 1132 -> 1022 kB (Bucket G)


### Testing
- Coverage for all 3 Settings classes (24 tests)
## [v0.1.0] - 2026-05-10


### Bug Fixes
- Wave-2 merge — ZoteroSection test ambiguous label query
- Topic fan-out should be no-op until per-user topics ship
- Remove user_id=None fallback from scheduler fan-out
- Defensive user_id resolver + migrations 69/70 deferred (Phase 2 final integration)
- Expose admin/auth/setup in routers package __all__ (Phase 2 hotfix)
- WS-2D antagonistic-completion sweep — schemas, IDOR, schedulers, sources, recommender
- Apply Wave-2 audit IDOR fixes to citations/notes/pdf/pulse/rag/zotero (Phase 2 WS-2D partial)
- WS-2A antagonistic-review fixes (Phase 2)
- Pyright cleanup in test_pulse_scoring_stage2 (Phase 1 followup)
- Antagonistic-review fixes for multi-user rollout
- Investigate and harden against agent silent-revert (Phase 1 WS-1)
- Three real bugs surfaced during E2E smoke
- Wire lookback_days, AdvisoryLock, and full-deck stale fallback
- Makefile sources versions.env + fix body font-sans→font-serif
- Update pulse training tests for Wave-4 HMAC signing
- Add await_args is not None guard in pulse scheduler tests
- Update probe-59 tests after W1-13 removal
- IDOR — thread user_id through analytics/contradictions/analyze/pdf/zotero/pulse
- Query key fix, errorMessage helper, a11y improvements, scheme guard, noopener
- Nginx hardening, HSTS at Caddy, CORS http local, cloudflared pin, langfuse healthcheck, secrets
- Daily_log user_id filter, generate_cards IDOR, deadline datetime types
- Remove stale probe-59, add table_schema filter to probes 60/61, add user.timezone to probe-57
- Add API-key headers to review_handler, rate-limit review_start, strengthen docstrings
- Migration-062 guard, Confidence.NONE enum, ctx_shim docstring
- Guard rehydrate against LiteLLM 'No DB Connected' + lock W4-2 marker
- Repoint stale qwen3-embedding:4b assertions + filter noop.test
- Recover W3-DRY-1 reranker work + repoint shim callers
- Focus clamp + rate-limit guard + auth-before-ack + bidi-sanitise
- Register noop.test task when JARVIS_ENABLE_TEST_JOBS=1
- Align init.sql with post-migration schema for fresh installs
- Null-value confidence guard + qwen3-embedding:4b dim
- Drop inner TLS hop, run Caddy→nginx over HTTP
- Mount LITELLM_MASTER_KEY + JARVIS_CONFIG_KEY + Langfuse via Docker Secrets
- Align daily_intent.user_id with project INTEGER NULL convention
- Re-check paper ownership at top of worker handlers
- Widen migrations_runner conn type + system.py Role import
- Log_focus_session SQL references dropped 'status' column
- Rename teststrip_*->test_strip_* + export from jarvis_common
- Disable Qwen thinking on smart/fast LiteLLM aliases
- Drop future-annotations to resolve Pydantic v2 ForwardRef
- Load PT Serif, IBM Plex Sans, IBM Plex Mono for type-pairing presets
- Add --cta-warn-* tokens for Research Feed trash banner
- Clean up diagnostics and test failures from parallel groups
- Fix 3 test regressions from Group 1-3 changes
- Apply marathon completion audit findings (waves A–D)
- Harden analyze and pulse closeout
- Close audit gaps and restore arxiv pulse
- Harden pulse diagnostics and retrieval eval
- Explain source-exhausted decks
- Complete phase c and model lifecycle
- Degrade badge now reflects latest run, not latest failed run
- Use LITELLM_MASTER_KEY as api_key in instructor client
- Remove double instructor-wrapping in call_llm_structured
- Replace pgrep with python os.kill(1,0) for telegram_bot
- Normalize :latest suffix in ModelSelector before calling onChange
- Strip :latest suffix before model comparison to prevent spurious YAML write
- Restore explicit bash for qdrant /dev/tcp check
- Restore Ollama bash healthcheck + init-secrets bootstrap
- Fix to init-secrets script
- Bust COPY layer cache on make rebuild
- Add Regenerate button when deck exists with 0 papers
- ScoreStack signal aliases + Pomodoro quick wiring
- Pairing entropy + global cooldown, prompt-safety doc, lifecycle xref, pulse stage2 canon
- Honour 429 Retry-After once + defensive .get() chain
- SecretStr core settings, plaintext-secret migration, internal-API doc
- Mask trailing chars + key rotation via _OLD env
- UTC streaks, request-ID sanitisation, audit metadata cap
- Timeout passthrough, structured summarization, observe coverage
- Doc drift, dead code, BIDI fix, scheduler procrastinate bypass
- Tighten My Day frontend and clean up dead env vars
- Close 7 IDOR gaps — Group B audit sweep
- D1-D5 audit sweep — tx discipline, type drift, lint hardening
- Unify route lookups + cutover POST /api/jobs to procrastinate (Bug 2)
- IntentSection auto-collapses footer + toasts on reopen
- Eliminate Appearance FOUC via inline pre-React script
- Accent presets override shadcn tokens + serif headings + Legacy type preset
- HeroPulse clamps/resets currentIndex on deck change
- BulkToolbar responsive margins + flex-wrap
- MyDayPage hash-scroll uses rAF retry loop
- ScoreStack uses 4 distinct hues + tooltips + h-2 height
- Wire Tailwind fontFamily to var(--font-*)
- Add psycopg[binary] to satisfy procrastinate 3.x runtime
- Use unambiguous submodule import in test_task_registry
- Pyright cleanups for process_batch endpoint
- Add explicit import importlib.util to satisfy pyright
- Add instructor/langfuse/openai deps + fix Wave 2.A test compat
- Fix InstructorRetryException import + type ignore for messages arg
- Shell bg-paper continuity — eliminate sidebar/main seams
- B4 — HeroPulse advances on rate + Pomodoro guard
- B3 — All-reading button → /feed?surface=library (no 404)
- B2 — Pomodoro chip scrolls to §Now section
- B1+U7 — IntentSection completed-row reopen + chevron toggle
- Add color: null to ProjectPulseItem mock — tracks contract type
- Shell bg-paper continuity — eliminate sidebar/main seams
- Mock window.matchMedia in jsdom setup for useThemeEffect
- PulseRow display rank from list index, not gappy server rank
- Allow Google Fonts under nginx CSP for v5 type stack
- Post-review fixes — tooltip on disabled trigger + live clock
- Sync PulseSection stage2_top_k fallback 50 → 40
- Off→on transition guard in star_paper — no double zotero.push
- Drop orphan zotero.enabled read in _get_zotero_poll_config
- Post-deploy bug fixes + inbox source filter
- Drop redundant inner txn in bulk hard_delete arm (W1.7-E review)
- Pdf_workflow distinguishes torch OOM from embedding error (W1.7-G)
- _trash_paper idempotent on re-trash + state preconditions on save/skip/reading (W1.7-B)
- Pulse mutation hygiene — invalidate pulse-today + optimistic save (W1.7-C)
- ActionItemsCard remove duplicate "Expand to triage" trigger (W1.7-D)
- Row_to_feed_paper now emits state + state_before_trash (W1.7-A)
- Stop button stays visible after navigating back to in-flight Ask stream (W1.6 review)
- Preserve step+message in structured PDF error so Retry button works (W1.6 review)
- UX-E Library chip tooltips + (i)→label-hover swap (W1.6-E)
- UX-D Ask streaming preserved across navigation (W1.6-D)
- UX-C PulseCard Save toggle race + optimistic update (W1.6-C)
- UX-B FeedbackButtons untoggle no longer locks (W1.6-B)
- UX-A FeedPaperRow polish — colored badge + tooltips on all action buttons (W1.6-A)
- Production tsc errors in W1.5 — phaseRef narrowing + mock signature
- UX-B.1 propagate user_state, UX-C.3 reset offset on switch (W1.5 review)
- Profile.py SELECT DISTINCT + ORDER BY violation
- W1.1 follow-up — drop stale litellm_master_key references
- W1.1 follow-up — drop Bearer assertion in embed_error_handling
- W1.1 drop master_key for transparent loopback proxy
- W1.5 swap pulse_ratings -> recommendation_feedback + docstring
- W1.4 swap pulse_ratings -> recommendation_feedback in signals
- W1.3 swap pulse_ratings -> recommendation_feedback in training
- W1.2 require state='trash' precondition for restore
- WS-PA-W7 post-review polish (race fix + status doc accuracy)
- WS-PA-W7-D Wave 7 build fix + smoke results update
- WS-PA-W7-C.1 Settings tab URL sync
- WS-PA-W6 replace dropped pus.status with pus.state/pus.starred
- WS-PA-W2-followup fetchFeed maps LibraryFilter → backend view
- WS-AH2-A8 app_factory equal-length init/teardown contract
- WS-AH2-A7 HardDeleteModal mutation onError toast
- WS-AH2-A6 bulk selection clears on URL-driven surface change
- WS-AH2-A3 _simple_digest db_user_id parameter + scoping
- WS-AH2-A2 deck INSERT user_id column + DRY-1 substitution
- WS-AH2-A1 hard-delete reorder + title trim + DRY-1
- App_factory order + crypto typing + dynamic_update whitelist + prompt_safety polish
- Save/Dismiss double-answer + digest archived/dismissed guard + review delegation
- Multi-tenant readiness — bind user_id in user-state filters
- Hard-delete commit PG before Qdrant + predicate constants + restore docs
- Focus-session ON CONFLICT (paper_id, user_id) + live-PG regression
- WS8 build-time type errors — CountsBadge prop, FeedCountsResponse, EmptyState icon, Select narrowing, mock UserState
- WS8-B1.1 BULK-TXN-001 wrap per-paper bulk action in nested savepoint
- WS7 smoke — coalesce nullable user_state fields in response model
- WS7 preference no-clobber + collapse profile dual-branch
- WS6 boot blockers found during smoke test
- WS6-A4a bookmark mutation onError toast (M4)
- WS6-A2b weight upper-bound clamp + log warning (H3)
- WS6-A3a strip control chars in escape mode (M2)
- WS6-A1d migration 043 defensive constraint-name DROP (H5)
- WS-5B dynamic_update guard + item_key encoding + cron validation + bookmark toggle
- WS-5A weight clamping + list param caps
- WS-4 TS build errors + bookmark UI wiring
- WS-1E cursor protection on failure + decrypt logging
- WS-1G BIDI strip in escape_llm_text + verification consolidation
- WS-1F S2 graceful degradation + OpenAlex SSRF scheme + API key param
- WS-1C remove private handler re-exports + _HANDLERS test fixture
- WS-1A app_factory migration ordering + worker await + LE proxy fix
- WS-1B card_generator brace escape + FSRS AttributeError
- WS-1D paper_detail API key header + rate-limit callback feedback
- WS-7 fixup — 4 Sprint 4 test contract drifts
- WS-5A/1C residue from parallel-execution stash race
- WS-1E source plugin defense (PI-EDGE-007/008/014)
- WS-1F zotero_service hardening (PI-EDGE-009/011/013)
- WS-1G settings rate limit + jobs SYM-002 (SEC-105 + SYM-002)
- WS-1D embedder chunk offsets + hybrid pagination (PI-CORE-005/006)
- WS-1A jarvis_common one-liners (DRY-003 + JC-005/006 + SEC-107/108)
- WS-1C scoring + profile fixes (PI-CORE-008/010)
- WS-1B learning_engine fixes (LE-002/003)
- WS-B3 [JC-001 + PI-CORE-007 + WS-4 + WS-6 hygiene] reaper + extraction confidence + uvloop + tests
- WS-B2 [PI-EDGE-001 + PI-EDGE-003 + PI-EDGE-005] pagination + cap + cross-ref pre-filter
- WS-B1 [PI-CORE-001 + PI-CORE-009] savepoint per card + 0-deck warning
- WS-A3 [PI-CORE-002 + Sprint 3 polarity] direct-equality dedup + word-boundary regex
- P0 [SEC-102] pairing INSERT ON CONFLICT replaces no-op UPDATE
- P0 [PYRIGHT-001] widen notify_job_update conn type to PoolConnectionProxy union
- P0 [SEC-101 + SEC-103] enforce secret file chmod 600 + Makefile target
- P0 [NGINX-001] add client_max_body_size 50m for PDF uploads
- P0 [DB-002] add notify_jobs_update trigger to init.sql + reconcile script
- P0 [DB-001] renumber duplicate migration versions 037/038 → 040/041 + collision detection
- FE-001 ActionItems error branch + FE-002 hasChunks guard + TEST-001 mock URL
- AH-001 escape field attrs + SEC-002 redact SourceResponse
- JOB-001 heartbeat-based reaper + kind-scoped kill filter (migration 035)
- JOB-002 cancel queued + JOB-003 error dict + API-001 response_model
- SEC-003 header+ ING-001 assert + AH-002 clear on exception
- WS-4.2 decrypt encrypted_value column in _get_zotero_config
- Await async update_litellm_model + restore _config_lock
- Expose refresh_api_key_cache() for test monkeypatching
- PI-013 non-optional embedder; PI-015 atomic upload_pdf rollback; PI-016 drop BackgroundTasks import
- TG-002 cancelled task guard; TG-003 drop dup handler; TG-004 unconditional rate GC
- FE-001 isGenerating job-aware; FE-003 phase ref; FE-004 AbortSignal.any polyfill
- ING-001 batch ceil; ING-002 DB/embed ordering; ING-003 page boundary; ING-004 urljoin guard
- LE-005 response_model on focus/streak/my-day; LE-009 FOR UPDATE in log_focus_session
- Remaining LE-004/LE-006/LE-007 files (jobs.py, card_generator, generation, tests)
- LE-004 drop duplicate batch GET; LE-006 RuntimeError; LE-007 doc job_handler contract; LE-012 enqueue in txn
- FE-002 zoteroPollNow returns {job_id,status}; track via job-store
- PI-002 upsert_paper+enqueue with paper_id; PI-010 acquire() wrapper; PI-014 cron reschedule
- JC-001 remove double json.dumps in dynamic_update (JSONB codec handles serialisation); remove compensating json.loads on readers
- EXT-001 prompt injection via wrap_delimited; EXT-002 drop unverified fields
- TG-001 escape nudge fields before HTML interpolation
- JC-002 wrap secrets OSError; JC-004 progress: float | None
- SEC-004 CORS default https://localhost:3001 matches paper_ingestion
- LE-003 add response_model=BatchAcceptedResponse on single-generate
- Push_paper_to_zotero uses project_papers table
- Paper_jobs None guards + _xml_safe lxml import
- Explicit float cast resolves Tensor/float return type mismatch
- Scheduler refresh_recommendations import + routers __init__ relative import
- Export zotero submodule from routers/__init__.py
- Suppress PointIdsList arg-type error in pdf_workflow
- __all__ for verification/decomposition, canonical Embedder imports
- Add __all__ to canonical files, fix pyrightconfig.json paths
- Canonical Embedder import + health type annotation
- Shim __all__ re-exports, ingestion imports, type fixes
- From-exc chaining, sanitized HTTP details, silent-swallow logging
- Eval_retrieval imports — drop stale scripts._paper_ingestion_imports
- G1 follow-up — restore needs_processing to onboarding_stage Literal
- Wave I1 follow-up — update stale docstring in test_papers_router
- Wave I2 — restore partial indexes dropped in earlier waves
- Wave F3 — handle non-dict LLM response; promote swallowed debug log to warning
- Wave H2 — propagate X-Forwarded-Proto; wire ProxyHeadersMiddleware
- Wave F2 — log errors in silent except handlers
- Round 11 Wave D — cron pre-validate + inline JARVIS_API_KEY read (B-M-04/D-07/H-04)
- Round 11 Wave D — verification fallback + local_source signature (B-H-01/B-H-02)
- Round 11 Wave D — pulse stats order + interval param + executive tz-date (C-M01/C-M02/E-04)
- Round 11 Wave D — pdf redirect urljoin + entity chunk_id (B-M-02/B-M-03)
- Round 11 Wave D — re-raise CancelledError after marking job cancelled (A-06)
- Round 11 Wave C — pass ctx=None in batch loop (E-02)
- Round 11 Wave B — strip BIDI isolates U+2066..U+2069 (A-04/A-10)
- Round 11 Wave B — refuse unauth internal API in DEV_MODE (F-01)
- Round 11 Wave B — widen _score_one except tuple (C-H01)
- Round 11 Wave A — SSE reconnect self-abort (G-01) + [DONE] loop escape (G-02/G-04)
- Round 11 Wave A — migration 026 idempotent (A-03 fresh-install crash)
- Remove fsrs.CardDict import removed in fsrs 5.x
- Wave D LOW fixes — import hoisting, cron default, tz validation, setup hardening
- Use call_llm(system=...) kwarg for Stage-2 scoring
- Strip BIDI overrides and zero-width chars in wrap_delimited
- Pulse_ratings unique constraint + deck-membership guard
- Enforce job ownership in get/list/cancel; remove user_id from request body
- Validate pulse cron next_run_time is within 366 days after reschedule
- Live nudge reload via internal HTTP endpoint + timezone-aware CronTrigger
- Clamp per_source_cap to stage2_top_k*2 / num_sources
- Replace asyncio.get_event_loop().time() with time.monotonic()
- Rename counter to saved_by_full_text_verify
- ActionItemsCard failedJobs selector returns stable memoized array
- Tsc -b build errors in tests + LibraryFilters readonly array
- Pyright optional-access guards and module-stub type ignores
- DEV_MODE-gate noop.test, add stale-job reaper, register card handlers explicitly
- Paper.process passes _SubCtx to run_process_pdf for sub-progress
- ?action=process scroll awaits data load
- Hydrate() also subscribes to queued jobs on page load
- Generate_cards_core raises JobError instead of HTTPException
- Reject empty source_types list (422 from Pydantic + disabled Search button)
- Wire Generate-Pulse button to job-store with 429 handling
- Scale JobsIndicator progress to 0-100 for Progress component
- Friendly Timezone label + tooltips on notification rows
- Proper source display names, human-readable notification labels, S2 key_env seed
- Add retry button on upload error; add Continue button to API Keys wizard step
- Remove duplicate processPdf export introduced by T7
- Remove double-encoding of signals and stats JSONB fields
- Migration 022 cleaner approach, fsrs pin, TS strictness fixes
- Align page titles — Research Feed + Home
- Correct empty state copy — no papers selected, not no templates
- Remove (CoSE) jargon from Force-directed layout label
- Tab overflow → horizontal scroll, Ingestion → Models & Notifications
- Replace vacuous decomposition regression tests [T1-fixup]
- W4.8 hardening gaps [M6,M27,M39,M41,M44]
- Inject now, count real LLM calls, PubMed sort, deck batch fetch [W4.7, M12-M14,M16]
- Fix Optional-access flood in telegram_bot handlers [M42]
- SSE 401 triggers logout; apiFetch combines abort signals [H9,H10]
- Extract security headers to snippet; add CSP additions [M32,M33]
- Commit rate-limit source changes + fix query.data null check [H11,M5,M3]
- Always start scheduler; /health returns 503 when degraded [H14,H15,H17]
- Learning_engine /health returns 503 when degraded [H17]
- Release DB connection before HTTP download; per-file conn in scan [H12,H13]
- Learning_engine /health returns 503 when degraded [H17]
- Guard aws CLI availability before S3 upload [H18]
- Resolve Wave 2 diagnostic issues across test files
- Wire QuoteVerifier strict-skip for all KG edges [C1]
- Existing-owner check, SHA256 hash in logs, rate limit on PAIR_ branch [H3,H4]
- Bounded pg_advisory_xact_lock with 60s timeout [H16]
- XFF walk-left + CF-Connecting-IP support, TRUSTED_PROXY_CIDRS env [H7]
- Move API key to sessionStorage, logout clears jarvis-ui [H8]
- %d→%s for nullable chat_id, compose-aware cert vol reset, fresh limiter in rate-limit test
- Suppress false-positive on limiter import + rename _app fixture to app_fixture
- BotConfig.telegram_chat_id optional + resolve_owner_chat_id across 15 outbound sites
- LAN port dedup via DASHBOARD_BIND_HOST, SAN cert propagation, tunnel CORS hostname
- Wrap create_pairing in transaction + expire-only sweep + rate limit 10/min
- Drop third-party QR + rebuild local services in update.sh
- Use https scheme + propagate LAN IP to CORS_ORIGINS
- Prevent apscheduler.triggers.cron stub pollution; fix resolver unused params
- Pre-import apscheduler in conftest to prevent test_pulse_scheduler stub pollution
- Use set[str] instead of frozenset for Pydantic model_dump include param
- Scheduler always starts pulse job, live reschedule, pdf_resolutions NULLS NOT DISTINCT (F2.2)
- Remove invalid type annotation on app.state.sources (F2.1 follow-up)
- Source plugin rate-limits, error handling, discovery cache (F2.1)
- Resolver Optional return type + lxml etree import annotation
- Topic description max-length (1000 chars)
- Rating mutation UX — no deck refetch, disable rated buttons
- HTML-escape paper URLs in formatters
- Register /pulse_now command handler
- Stage1 author-bonus uses dual set (names + S2 IDs)
- Stage2 LLM scoring — switch to smart model, bump max_tokens
- Validate pulse.* config keys on PUT + frontend cron gate
- Weekly_summary SQL column (status not user_state, starred not saved)
- Persist_deck counts actual inserts, wrap in transaction
- Harden PubMed XML parser against XXE (use defusedxml)
- Layer 3 review fixes — cron UX, rating invalidation, Escape key
- Stream G quality review — SQL interval, FK handling, stats logging
- Register openalex + pubmed source plugins via package __init__
- F0 review nits — FLOAT consistency, stream C fixtures, imports
- Audit round 4 — security, correctness, schema sync
- Audit fixes + executive function polish (Phase 1+2)
- Remove duplicate lines and format MyDayPage
- Code audit round 6 — 10 verified fixes across 9 files
- Code audit rounds 3+4 — 35 fixes across 27 files
- Allow KaTeX data: fonts in CSP header
- Force-split oversized chunks exceeding embedding context window
- GPU support for Marker + Docker infra hardening


### Documentation
- Commit 2026-05-09 full-codebase audit (3A triage decision: keep active)
- Refresh for Phase 1+2 + Sprint A/B + Wave 2 (Bucket E1)
- Format-watcher silent-revert investigation (Bucket E3)
- Sprint A (Telegram pairing) + Sprint B (canonical corpus refactor) plan
- Append WS-2E status to multiuser-rollout plan
- Wave-2 multi-tenant audit results (Phase 1 WS-3)
- Document auth-before-ratelimit ordering in paper_commands (W3-14)
- Ratify 06-hardware-aware-settings (user gate cleared)
- Add 06-hardware-aware-settings (Wave-4 contract design)
- UI heading-rhythm audit (input for next deep-plan)
- D6 reranker matrix complete + final promotion decision
- D7 promoted — qwen3-embedding:4b nDCG@3 80.0% vs 64.1%
- UI/UX gap report + Phase 1g fix plan
- D8 comparison report — D7 infeasible, D6 deferred
- Add OI-1 open issue — idempotent migrations / generate_series pre-seeding gap
- Add second-machine setup, Tailscale iOS HTTPS, troubleshooting rows
- Mark shipped items in phase-1f spec (2026-05-04 partial ship)
- Add Pomodoro+ScoreStack+Intent design spec; update v5-sweep
- Add contract 05 — model lifecycle
- V5-sweep proposal placeholder
- Save Marathon continuation plan to canonical in-repo path
- Re-order CHANGELOG so [1.6.1] sits above [1.6.0]
- Phase C embedding-model upgrade plan
- B.3 reranker production note + NEW-H2 audit closure
- Procrastinate job broker migration spec
- Persist Phase 1b + 1c plans to docs/plans/
- Add Phase 1a implementation plan for My Day redesign
- Changelog [1.5.0] + plan-header SHIPPED flags for contract wave 1
- A.5 rollup — 01-settings.md dispositions for Wave 1 Settings cleanup
- Add evergreen contract layer for Settings/Pulse/LLM/Observability
- Anti-drift coordination contract — My Day redesign + Marathon Phase B
- Factual currency fixes for pre-META-handoff polish
- Add CHANGELOG [1.4.3] entry + persist W1.7 plan to docs/plans/
- WS-PA-W7 + Phase A MARATHON COMPLETE at ee1de7f
- WS-PA-W7-E.1 status doc + CHANGELOG — Wave 7 closeout
- WS-PA-W7-C.2/C.3 audit B6/B7 falsified at HEAD
- WS-PA-W7-A contract restoration — spec amendment 7 + cutover doc fixes + archive
- WS-PA-W6 live smoke results — 1 BLOCKING fixed, 2 NON-BLOCKING
- WS-PA-W4+W5 DONE at 6cdaa15 + 1b46f6a — Next: Wave 6 verification + push + merge
- WS-PA-W5 lifecycle redesign closeout — legacy contract docs deleted; CHANGELOG [1.3.0]; all referrers updated
- WS-PA-W3 status doc — Wave 3 DONE at 25e8c17
- Promote marathon META plan into repo
- WS-PA-W2 status doc — Wave 2 DONE at ffba7a1
- WS-PA-W1cd status doc — Wave 1cd DONE at b28c34d
- WS-PA-W1ab status doc — backfill 4b60ca9 test repair + process lesson
- WS-PA-W1ab commit SHA backfill in Phase A status doc
- Phase A multi-session runbook + living status doc
- WS-AH2 fix plan + 2026-04-30 verification audit report
- WS-AH1 audit-hotfix-sprint plan + falsification record
- WS7 closeout — audit reports, future-import analysis, residuals, changelog
- 2026-04-29 test amendments for boot reliability
- WS6-B4 fix drift in README + PaperHeader test
- WS6-A3b ensure M1 entry covers pulse_ratings + paper_user_state writes
- WS6-B1c CHANGELOG 1.2.5+1.2.6 + DEPLOYMENT (ZT Access + CF flag) + residuals
- WS6-B1b truth-up PRD + REQUIREMENTS (counts + secrets + Sprint 5/6 entries)
- WS6-B1d module READMEs (db migrations + secrets + n8n optional)
- WS-7 Sprint 5 audit truth-up + residuals + H6 signature closeout
- WS-7 Sprint 4 audit truth-up + residual risks
- WS-D close out post-R14 plan + audit remediation status + deferral notes
- WS-8 awscli graceful skip + header docs + env.example recipe
- R14 search UX notes
- Fix remaining stale app/ paths in AGENTS.md and README.md
- Add Zotero integration to PRD.md and REQUIREMENTS.md
- Sync R12 — package rename + canonical test commands
- Wave 0 sync — CLAUDE.md migrations, archive R10 report, R11 memory, desloppify note
- Add Round 9 audit reports; archive rounds 6-8 + UX audits 1-4
- Add JARVIS_TRUST_CF_CONNECTING_IP to .env.example; fix XFF comment
- Clarify jobs SSE endpoint serves both services via shared table
- UX Round 4.5 walkthrough captures
- Final UX Round 4 documentation pass (README, AGENTS, PRD, REQUIREMENTS, CHANGELOG, CLAUDE)
- Add rule #11 — prompt-safety primitives are mandatory for untrusted LLM input [S-2.4]
- Add combined code + security review report (2026-04-14)
- Add KG entity extraction rule #10 [W2.1, C1]
- Import audit reports + setup design spec; ignore .gemini and docs/personal
- UX round 2 — webapp simplification + telegram T1
- Rewrite quick-start around setup.sh + add troubleshooting (C2)
- Add Claude behavior optimization design spec
- Fix stale README gaps, add LAN/Telegram/troubleshooting sections
- Add [1.2.1] post-audit hotfix + HIGH fixes entry
- Add Discovery & Pulse design specification (working scratch)
- Embed Discovery & Pulse subsystem design into persistent docs
- Sync all documentation with codebase + move n8n to profile
- Enforce dockerized dev workflow and document recommendation engine


### Features
- Extended sidebar health indicator + collapsed pill (Bucket D3)
- Zotero group-library UI
- Zotero group-library config keys
- Library_type + group_id support in ZoteroClient
- Progressive disclosure + spark-line + presets + search (Bucket D1)
- Telegram pairing settings UI
- /pair /unpair /whoami + 6 orchestrators user-scoped
- /api/telegram/{pair-token,pairing} endpoints
- X-Owner-User-Id helper with API-key + CIDR allowlist gate
- Migration 071 — telegram pairings + tokens (Sprint A)
- Topic-based fan-out into user_library
- Migration 072 canonical corpus + jarvis_common.library helper
- Bootstrap scripts + first-run web wizard + README rewrite (Phase 2 WS-2F)
- First-login tour for new users (Phase 2 WS-2G)
- Admin user management API + UI (Phase 2 WS-2B)
- Admin-only access to /logs and /api/logs/* (Phase 2 WS-2E)
- Migration 070 — user_id columns for cards/decks/tracked_authors/etc (Phase 2 WS-2D)
- Magic-link auth foundation — users, sessions, magic-link tokens (Phase 2 WS-2A)
- Make QuoteVerifier mandatory in Pulse scoring + Weekly digest (Phase 1 WS-2)
- System Logs sidebar, dashboard port 3010, expanded roadmap
- Restore HTTPS on localhost:3001 via Caddy + mkcert
- Deferred-items punch list (post-sweep cleanup)
- Pulse reliability + Logs admin UI + Caddy mkcert (3-workstream sweep)
- Wave-4 — HMAC-sign pickle blobs in pulse classifier training
- Wave-3C — activate user_id predicates in recommendations + dashboard_api
- Wave-3B — user_id predicates in LE projects/tasks/milestones routers
- Wave-3A — add user_id to paper_recommendations, projects, tasks, milestones
- Wrap_delimited returns truncation flag; log in summarization
- Add ESLint v9 flat config with TypeScript + a11y rules
- Hardware readout + num_ctx slider + thinking toggle (T3-C)
- Hardware-aware fit math + thinking-mode propagation (T3-B)
- Add hardware-aware fields to model catalog (T3-A)
- Font-sans override + § double-caption removal + a11y polish
- Optional backup encryption + cert renewal check
- Gate Qwen3Reranker behind RERANKER_BACKEND flag
- Rate-limit /api at the edge (30 req/min per IP)
- Audit-log every successful DELETE in LE routers
- Defense-in-depth streaming <think> filter
- Promote qwen3-embedding:4b to production embed alias
- Promote qwen3-embedding:4b to assignable/default
- Wire Today's Intent textarea to learning_engine backend
- Wave 1 — UI/UX gap fixes + Today's Intent backend
- Wave 0 — design tokens for timer, warn CTA/badge, project rotation
- V5 sweep on Analytics (§ markers, Card flatten)
- V5 sweep on Home page (§ markers, Card flatten, status tokens)
- Add § markers, TabsList underline, Card flatten for SettingsPage+TopicSection+TimerSection
- V5 sweep on Settings (§ markers, Card flatten, underline tabs, status tokens)
- V5 sweep on Projects (§ markers, Card flatten, underline tabs)
- V5 sweep on Research Feed (§ markers, custom pills→underlined tabs, Card flatten)
- V5 sweep on Paper Detail (§ markers, Card flatten, underline tabs)
- V5 sweep on Learning Cards (§ markers, Card flatten, underline tabs)
- V5 sweep on Extraction Table (§ markers, Card flatten, status tokens)
- V5 sweep on Citation Graph (§ markers, Card flatten)
- V5 sweep on Knowledge Graph (§ markers, Card flatten, status tokens)
- Add reranker stage to retrieval eval harness (D6-A)
- Add Qwen3-Reranker generative adapter (D6-B)
- Warmup probe + REEMBED_REQUEST_TIMEOUT env (D7-prep)
- Group 3 — setup banner, fallback badge, FavoriteTopicsPanel, baseline eval
- Group 2 — hardware auto-configure hook, RERANKER_MODEL env-var, REEMBED_COLLECTION
- Group 1 — Ollama :ro fallback, Tier 4 catalog, fallbacks, eval set, feedback analytics
- Migration 057 — seed default llm/fsrs config for existing installations
- Last-mile bring-up sprint — docs, SecretStr, smoke-test fixes
- Wire LITELLM_MASTER_KEY to gate LiteLLM admin endpoints
- Join job_progress in procrastinate adapter
- Wire update_progress to job_progress and is_cancelled to should_abort
- Add migration 054 for job_progress table
- Drop legacy jobs table, collapse to procrastinate-only
- Delete legacy worker — _HANDLERS, job_handler, enqueue, worker_loop removed
- Migrate 18 job kinds from legacy enqueue to procrastinate defer_async
- Digest.weekly canary cutover to procrastinate (Step 3.1)
- Procrastinate worker in service lifespans (Step 2 part 2)
- SSE bridge for procrastinate-side job events (Step 2 part 3)
- Task_registry with 19 procrastinate task stubs (Step 2 part 1)
- POST /api/papers/process_batch endpoint
- Procrastinate dep + migration 052 (Step 1 of cutover)
- Langfuse dashboard link card (B.1+B.2 follow-up)
- Migrations 050+051 + journal CRUD endpoints
- Primitives — WhyChips, HashtagChips, GradientProgressBar
- Delete call_llm + cutover decompose_query + docs
- Extraction entities + core → call_llm_structured (Wave 2.B)
- Card_generator.py → call_llm_structured + CardGenerationOutput model
- Weekly_summary.py → call_llm_structured + WeeklyDigestOutput model
- Contradictions.py → call_llm_structured + ContradictionClassification
- Hide YesterdaySection + EndOfDaySection — no backend data yet
- Instructor + Langfuse foundations + pulse scoring canary
- DateMasthead — 36px date, 14px MiniStat, wider tracking, quote attribution
- TopBar v5 restructure — brand + search + avatar + bg-paper
- Swap reranker default to mxbai-rerank-base-v2
- ScoreStack — ink-blue gradient + emb·llm·rec·g badge
- Appearance panel — accent / type pairing / density presets
- Journal section + resume reading hero + project color badges
- IntentSection empty-state + PulseRow hashtag chips + tags type
- Hide YesterdaySection + EndOfDaySection — no backend data yet
- DateMasthead — 36px date, 14px MiniStat, wider tracking, quote attribution
- ProjectsSection — gradient bars + status dot logic + milestone row
- HeroNow — custom segmented control + remove Resume tab
- HeroTask — project badge + priority + timer enrichment
- HeroPulse — WHY chips + 26px title + ~6 min meta
- Primitives — WhyChips, HashtagChips, GradientProgressBar
- TopBar v5 restructure — brand + search + avatar + bg-paper
- ScoreStack — ink-blue gradient + emb·llm·rec·g badge
- Dark-mode sweep for top-of-page sections (4 files)
- Dark-mode sweep for bottom sections + primitives (5 files)
- Dark-mode sweep for mid-page sections (3 files)
- Wire useThemeEffect in AppShell + dark sweep for layout shell + MyDayPage
- Wire useThemeEffect in AppShell + dark sweep for layout shell + MyDayPage
- ThemeToggle (Sun/Moon/Monitor) in TopBar
- Migrate v5 tailwind tokens to CSS-var refs + add text/border utility colors
- Pre-mount inline script applies stored theme before first paint
- Add theme-store (Zustand persist) + useThemeEffect hook
- Wave 3 — MyDayPage rewrite + delete obsolete components
- Wave 2.8 — LearningFocusSection
- Wave 2.7 — TriageSection
- Wave 2.6 — TodaysPulseSection + PulseRow
- Wave 2.9 — EndOfDaySection stub
- Wave 2.4 — IntentSection + TaskRow
- Wave 2.3 — Hero family (Now/Pulse/Task/ResumeReading)
- Wave 2.5 — ProjectsSection
- Wave 2.1 — DateMasthead section
- Wave 2.2 — YesterdaySection placeholder
- Wave 1.3 — HeaderPomodoro shows active task title
- Wave 1.2 — SectionHeader + ScoreStack primitives
- Wave 1.1 — type stack, design tokens, tailwind extensions
- Delete 5 GHOST user_config keys + wire fsrs.learning_steps + drop fsrs cache
- Split last_error vs degraded_reason badge + add conditional-signal tooltips
- Wire zotero.auto_push_on_star — auto-enqueue Zotero push on star
- Bulk Delete Forever + Select All + Reading List label fix (W1.7-E+F)
- UX-I structured PDF errors + Stage 2 LLM scoring progress granularity (W1.6-I)
- UX-H My Day tactical compact pass (W1.6-H)
- UX-G Notes UI disambiguation — Quick Rating / Annotations (W1.6-G)
- UX-F Analyze Paper smart-mode + structured error UI (W1.6-F)
- UX-B per-card UX polish — untoggle, badges, tooltips, refresh feedback (W1.5)
- UX-D RAG/Ask polish — single loader, AlertDialog clear, unmount abort, single-turn doc (W1.5)
- UX-C bulk toolbar polish + pagination on Inbox/Library (W1.5)
- UX-A add Pulse Deck + Ask sidebar entries (W1.5)
- UX-E add DELETE /feedback + PUT /unsave for Pulse-card untoggle (W1.5)
- WS-PA-W7-B contract gap closure — code
- WS-PA-W3 callback rewrite + digest state ENUM + Pulse-aligned commands
- WS-PA-W2.3 pages + RejectedTopicsPanel + l2_lambda + keyboard remap
- WS-PA-W2.2 shared components — lifecycle buttons + FeedbackButtons
- WS-PA-W2.1 types + API client for new lifecycle + feedback CRUD
- WS-PA-W2.0 _upsert_recommendation_feedback populates topic_id
- WS-PA-W1cd L1+L2+L3 backend learning + Zotero state='to_read' + discovery_origin stamping + feedback CRUD
- WS-PA-W1ab lifecycle endpoints + state-based predicates + counts SQL
- WS-PA-W0 schema gate — migrations 047 + 048 + 049 + init.sql mirror
- WS-AH bulk + keyboard + onError + URL guards + feed invalidation
- WS8-B2.9 PaperDetail action bar (Save/MarkRead/Archive/Dismiss/HardDelete)
- WS8-B2.9 PaperDetail action bar (Save/MarkRead/Archive/Dismiss/HardDelete)
- WS8-B2.4 FeedView component (replaces LibraryTab+NewTab) with surface-aware action callbacks
- WS8-B2.11 App.tsx /pulse → /my-day redirect (Pulse tab removed from /feed)
- WS8-B2.8 useFeedKeyboardShortcuts hook + KeyboardCheatSheet modal (j/k/s/S/e/d/r/o/Enter/?/Esc)
- WS8-B2.6 TrashView + HardDeleteModal (2-step title-confirm + also-zotero coming-soon)
- WS8-B2.10 CountsBadge component (reactive count next to surface chips)
- WS8-B2.3 ResearchFeedPage surface chips (Inbox|Library|Search|Ask|Trash) + sub-chips + Pulse removal
- WS8-B2.1 lib/api.ts paper-lifecycle mutation clients + useFeedCounts hook
- WS8-B2.2 types — UserStateResponse adds saved/dismissed/updated_at + SurfaceView/BulkAction/FeedCountsResponse
- WS8-B1.9+B1.10 digest filters + /inbox command + Save/Dismiss callbacks
- WS8-B1.1+B1.2+B1.4 papers router /save /unsave /dismiss /restore DELETE /bulk /feed/counts + bookmark/archive lifecycle wiring
- WS8-B1.5+B1.7 rate_card lifecycle semantics + generator excludes archived/dismissed candidates
- WS8-B1.8 delete_paper_vectors helper for hard-delete vector cleanup
- WS8-B1.3 feed_query view= predicate mapping for inbox/library/trash/etc
- WS8-B1.6 recommender excludes dismissed (Trash) candidates
- WS8-B0.1 migration 046 paper-lifecycle triage axes (saved/dismissed/updated_at + backfill)
- WS7 FeedPaperRow + archive UX + ContradictionsPanel states
- WS7 candidate-key preview + batched sources + ASCII normalization
- WS7 per-user flags + status no-collapse + functional indexes
- WS-3 migration 043 + ownership stubs doc + profile/RAG user_id threading
- WS-6B-β ownership wiring for rag/extractions/search/feed/discovery
- WS-6B-α ownership wiring for jobs/notes/papers (PI-EDGE-002/004)
- WS-6A migration 042 + ownership helper foundation
- WS-3 Docker Secrets full migration (DOCKER-001/002/005)
- WS-2 bookmark endpoint + telegram callback hardening (TG-001/002/003)
- WS-A2 [WS-2.1 + FE-004] inline sentence highlighting + open-redirect guard
- WS-A1 [PI-EDGE-002 + PI-EDGE-004] discriminated-union job payloads + idempotency + 404 path
- Add listen notify stream hygiene
- Add semantic ranking signals
- Promote verified annotation evidence
- Polish provider and model controls
- SEC-001 wire JARVIS_CONFIG_KEY through setup.sh + docker-compose
- WS-2.3 Pulse reasoning verification badge
- WS-6c ONNX backend for cross-encoder
- WS-2.3 reasoning + weekly-summary verification
- WS-2.2 PDF snapshot thumbnails in evidence UI
- Sentence-level answer verification with confidence SSE event (WS-2.1)
- RAG answer confidence badge on chat responses
- Cloud-provider API key injection via /config/update
- Providers Settings tab for cloud LLM API keys
- Config_crypto module + migration 033 for at-rest secret encryption
- PULSE-001 return source_counts per plugin; PULSE-002 stats.card_count from persisted rows
- PI-007 migrate batch_fetch to @job_handler; PI-009 papers list positional params
- PI-005 POST /api/zotero/poll; PI-011 BBT_BASE_URL env; PI-012 paginate collections
- Zotero discoverability hints + paper detail polish + tests
- Track external jobs + invalidate paper queries on zotero success
- Wire preview results + research feed navigation + drawer
- Search preview types + drawer + row components
- Preview endpoint + library match + structured source errors
- Phase E2 — APScheduler cron job for library sync
- Phase E1 — incremental library poll service
- E3 — un-gate sync toggle in Settings
- Router + trigger wiring
- Zotero frontend — Settings integrations tab + ZoteroPanel component
- Zotero client + push service with job handlers
- Wave L2 — structured audit log table + jarvis_common.audit helper
- Wave J — make sentence-transformers optional behind RERANKER_ENABLED
- Round 11 Wave C — document TELEGRAM_BOT_URL + skip reload when empty (H-01)
- Round 11 Wave UI — timezone combobox + notifications.* DRY cleanup (UI-1, UI-2)
- Exponential backoff on SSE stream reconnect (1s→8s ceiling)
- UX fixes + Round 8 audit remediation (8 parallel groups)
- Migrate extraction.batch to job handler
- Migrate papers.batch_process + papers.batch_summarize to job handlers
- Add user_id ownership filter to SSE stream endpoints (single-tenant no-op)
- Surface degraded_reason as typed field on PulseDeckResponse
- Migration 024 — partial index on jobs.user_id
- Multi-source checkbox UI + backend fan-out + update Library SOURCE_OPTIONS
- Tooltips on action buttons + expandable workspace note
- Redesign as triage dashboard with DayHeader, PulsePreviewCard, ActionItems
- Frontend job-store + TopNav JobsIndicator + toast notifications
- Migrate generation to jobs + clickable action_link errors + PaperDetail ?action=process
- Migrate paper.process + paper.analyze; fix local-paper analyze pdf_url bypass
- Migrate generate to jobs + degraded vs fatal distinction + /api/pulse/debug
- Multi-source fan-out with per-source error isolation + PubMed sort fix + S2 author filter
- REST routers + service lifespan workers + unit tests
- Drag-to-reorder sources + uniform card layout + info bubbles
- Display_order backend + PATCH /api/sources/reorder
- Migration 023 + jarvis_common jobs module foundation
- Global Pomodoro in TopBar + sidebar Research Feed + My Day title
- Add Source API Keys step to setup wizard (step 5 of 7)
- Drag-drop PDF upload zone replaces manual file path instruction (F-03)
- Add year/author/sort filters to search-preview (F-05)
- Extract TimeSelect shared component; fix Automation labels (F-08, F-09)
- Inline API key edit for all sources, remove env var blocks (F-07)
- PulseDeck description text + ProjectPulse rename and deep-link (F-01, F-02)
- Render StreamingChat directly in Ask tab, remove CrossPaperChat collapsible (F-06)
- Rename jargon labels to user-friendly copy (F-04)
- Restructure Research Feed into 5 isolated tabs (F-11 T2)
- Workflow banner and tooltips on extraction table page (F-05)
- Deadline edit popover with urgency color coding (F-10 T1)
- Add template section description and format hint (F-04 T1)
- Add sources description and priority tooltips (F-01)
- Boolean toggles and constrained number inputs in ingestion config (F-02)
- Remove cron disclosure, replace time inputs with 24h selects (F-03)
- Add description and tooltips to Authors section (F-08)
- Add task delete/reopen actions, project badge on completed, deep-link (F-09)
- Add section labels, sort direction hints, library count (F-11 T1)
- Add labels and tooltips to Topics edit form (F-07)
- Add per-slider tooltips to Pulse weight sliders (F-06)
- Promote Analyze Paper, collapse manual steps, expose step tracker, Max cards tooltip
- Add InfoTooltip to graph depth, min-paper-count, retention trend, retention tile
- Normalize to 1.0 button + Pulse context link
- Pulse time picker, deck/top-k sliders, Background Paper Search rename
- Remove Quick Navigation, add Batch Ops confirmation dialogs
- Consolidate metric tiles to 5, Library subtitle, Nudges→Scheduled Jobs
- Hide pulse/setup/telegram keys, add Paper Workflow group, FSRS tooltips
- Rename Unscored→Not yet ranked badge, add Pulse tooltip
- Close 22 audit findings — 1 CRIT + 19 HIGH + 2 MED
- Add escape_llm_text/wrap_delimited + apply to 4 LLM call sites [S-2.4]
- Migration 021 tracked_authors uniqueness + tombstone resolver.py [W4.7, M7,M15]
- Batch_generate_cards returns 202 + background task [M19]
- Add rate limits to recommendations, setup-status, telegram commands [H11,M5,M3]
- First-run setup wizard + Integrations tab (B1)
- ./setup.sh + ./update.sh one-shot installer (A4)
- Setup-status + telegram pairing endpoints (A1)
- Deep-link pairing + DB-fallback auth (A2)
- Setup-status + pairing types/api (A5)
- Pin 3rd-party images + cloudflare tunnel profile (A3)
- Per-route error boundaries
- Stream L complete — eval harness + Playwright E2E
- Stream L — eval harness for scoring pipeline
- Stream K complete — Telegram bot thin delivery + rating callbacks
- Stream K — gut research_pulse to thin delivery layer
- Stream J complete — settings extensions for Pulse
- Stream J — SourceSection dynamic key_env display
- Stream J — AutomationSection Pulse subsection
- Stream J — TopicSection description field
- Stream J — expose topic description in API and types
- Stream I complete — PulseDeck widget + MyDay/Feed integration
- Stream I — PulseDeck widget
- Stream H — PulseCard and WhyPopover with tests
- Stream H — InfoTooltip primitive with tests
- Stream H — types and API client for Pulse deck endpoints
- Stream G complete — scheduler wiring + smoke tests
- Stream G — REST router with 6 endpoints and tests
- Stream G — run_pulse 7-step orchestration with tests
- Stream G — discover_candidates fan-out + dedupe
- Stream D finalize — remove stale digest.py, wire rag router
- Stream B — source plugins (arxiv/S2 extend, openalex/pubmed new)
- Stream A — scoring core (profile, prompts, 3-stage pipeline, deck)
- Stream C — PDF resolution chain (arxiv → unpaywall)
- F0 foundation - migration 018 + PaperSource ABC extension
- My Day redesign + Pomodoro timer rewrite + UX polish
- Implement My Day unified checklist and focus buttons
- Add /focus and /next commands for ADHD execution support
- Add My Day and Focus APIs for executive function view
- Recommendation engine Phase 1 — liked centroid + project context
- Quality infrastructure — conftest fixtures, pyright, pre-commit, targeted tests
- Wire model selectors to LiteLLM config + chat progress UX
- Marker PDF parsing + KaTeX math rendering


### Miscellaneous Tasks
- Generate CHANGELOG for v0.1.0 and populate RELEASE.md
- Docker-compose image tags (JARVIS_VERSION variable)
- Generate CHANGELOG for v0.1.0
- Cliff.toml + CHANGELOG.md generation config
- Stronger post-commit blob-content check (Bucket E3)
- Unswallow frontend/src/components/logs/
- Cleanup + audit/desloppify outputs from prior agent sessions
- Wave-3D — annotate legitimate defer_async user_id=None sites
- Enable noUnusedLocals + noUncheckedIndexedAccess
- Ignore .codeboarding/ + commit deep-audit source doc
- Telegram healthcheck + langfuse pin + caddy limits + env-driven entrypoints
- Bump Ollama 0.17.7 -> 0.23.1 (D7-1)
- Apply low-severity review findings
- Drop accidentally-committed libs/jarvis_common/uv.lock
- Cleanup post-marathon — archive shipped plans, fix drift, tighten graphifyignore
- Archive shipped Phase A plans + consolidate plan dirs
- Rename unused full_text to _full_text in pdf_workflow (W1.7-G followup)
- Pre-Phase-A baseline (harness restructure + in-flight fixes + Phase-A planning artifacts)
- WS-AH2-A9+A10 anchor migration lint + archived-predicate guard
- Strip BEGIN/COMMIT from migration 046 + lint script (044+ scope)
- WS8-B0 tighten UserStateUpsert.status to post-046 enum + drop dead Zotero-on-starred-status branch
- Apply ruff-format to test_eval_retrieval_script.py
- WS7 lockfiles + marker filter + subprocess timeouts
- WS6 pyright warnings + README /review entry
- WS6-A1c canonicalize Embedder import in discovery + search (H4)
- WS-5C SYM-001 source error handling + DB-003 + DOCKER-003
- WS-C [FE-001/007/008/009/011/012] polish bundle
- OPS-001 enforce chmod 600 in docs + setup.sh
- Drop unused imports + fixture params for LSP cleanliness
- Remove dead _TELEGRAM_BOT_URL module-level constant
- Round 11 Wave E — 6 LOW cleanup + pyright unused-var tidy
- Prune stale screenshots + flip shipped statuses + fix migration counts
- Ignore .gemini and docs/personal
- Add CLAUDE.md, .graphifyignore, update behavior optimization spec
- Phase 1 Discovery & Pulse shipped


### Performance
- Bump _LLM_CONCURRENCY 5→8, drop _DEFAULT_STAGE2_TOP_K 50→40
- Rate_card early-return on rating='open'
- Use vr.matched_span_start for O(1) quote position lookup


### Refactoring
- Assert_paper_ownership reads discovered_by + user_library
- Replace papers.user_id predicate with user_library JOIN
- Drop user_id from upsert_paper; canonical-corpus only
- Promote paper_state helpers, funnel user_id resolvers, improve noop_task docs
- Consolidate QuoteVerifier in jarvis_common
- Consolidate migrations + record_terminal_outcome progress
- Rename _strip_think_streaming -> strip_think_streaming
- WS-AH2-A4+A5 archived predicate substitution
- Extract sse_event helper + SSE_DONE constant
- WS7 resolve_secret_row helper + cooldown branch test
- WS6-A5 D1/D2/D6/D7 single-file consolidations
- WS-5B split routers/search.py into discovery + feed (GOD-001)
- WS-5A extraction + rag subpackage migration (ARCH-001/002 + COMPLIANCE-002)
- WS-4B lifespan factory + verifier DI (DRY-002 + COMPLIANCE-001)
- WS-4A jobs router factory (DRY-001)
- WS-6b pydantic-settings for shared env vars
- JC-003 bucket reset; JC-005 cache api key; JC-006 request_id in errors; JC-007 raise not assert; JC-008 validate wrap_delimited tag; JC-009 public keepalive constants
- LE-010 explicit ProjectDetailResponse; LE-013 shared compute_streak; LE-014 acquire() sweep; LE-015 guard snapshot path
- LE-008 replace $N counting with dynamic_update / explicit positional params
- EXT-004 delete shadowed extraction.py; PULSE-003/004 dead code removal
- Replace assert with RuntimeError in production code
- Extract auto-pipeline, promote deferred imports (C7)
- Paper_ingestion/ root reorg into extraction/ rag/ subpackages (C6)
- ProgressContext Protocol, remove app back-channels, type fixes (C5)
- Extract migrations runner, telegram bootstrap, system endpoint (C4)
- Split 947-LOC Embedder into ingestion/ subpackage (C3)
- Split models.py god-file into paper_ingestion/models/ subpackage
- Prefix normalization, auth dedup, typed responses (C1)
- Drop B1 backward-compat helper aliases
- XML parser consolidation, rate-limiter naming, docstring trim
- Drop underscore-prefix on public helpers, db_pool naming
- Delete dead learning_engine/jobs.py + telegram command_handler barrel
- Rename top-level package app → telegram_bot
- Rename top-level package app → learning_engine
- Rename top-level package app → paper_ingestion
- Wave G4 + H3 — utc_now_iso helper; rate-limit batch_status SSE
- Wave F7 — lift hardcoded URLs to module-level constants
- Wave F5 — replace global keyword with class-based state holders and lru_cache
- Wave G3 — annotation tightening in pulse router + rate_limit + card_store
- Wave H1 — add TypedDict definitions for project/task/milestone payloads
- Wave D4 — drop pdf_workflow backward-compat aliases; update tests
- Wave G1+G2 — CardType enum in CardResponse; CrossPaperRagPrep dataclass
- Wave D3 — drop _insert_card alias; use insert_card directly
- Wave D2+D4+D5 — router-level verify_api_key; drop main.py re-export shim
- Wave F6 — ScriptError instead of sys.exit in library code
- Wave F9 — move duplicated router constants to jarvis_common
- Wave D1d — state→Depends(get_db_pool) for analyze/papers/pdf/rag
- Wave D1b — state→Depends(get_db_pool) for knowledge_graph/pulse/settings
- Wave D1a — state→Depends(get_db_pool) for authors/citations/extractions
- Wave D1c — state→Depends(get_db_pool) for jobs/telegram/topics
- Complete command_handler stub — remove duplicates, update test imports [T4-fixup]
- Split command_handler into handlers/commands/ package [CQ-6.1]
- Learning_engine type-safety + drive pyright to 0 [W4.2, M22-M26]


### Security
- Wave L1 — run containers as non-root; Docker Secrets for credentials


### Testing
- Group library coverage (Bucket C)
- Preset/expand/search/spark-line coverage (Bucket D1)
- Mock new intent api in IntentSection.test, last orange holdout
- Bring test_eval_retrieval_script.py in line with D6-A
- Add Qwen3Reranker tests + EVAL_RERANKER factory branches (D6-C)
- Guard mock_enqueue.await_args against Optional access
- Update mocks for Group B IDOR/star/feedback rewrites (remaining files)
- Update mocks for Group B IDOR/star/feedback rewrites
- Update litellm + pulse-scheduler tests for Groups C and G changes
- Thread user_id through training tests for Group B
- Align mask_secret assertions with H.1
- Align mask_secret assertions with H.1
- Align telegram pairing + sprint4_1a tests with H.1/H.10 changes
- Align fixture with UTC anchoring (post-H.5)
- Cover update_progress UPSERT, is_cancelled bridge, and JOIN
- Assert legacy enqueue not called when KIND_TO_TASK dispatches
- IntentSection reopen toast/auto-collapse + AppearanceSection legacy preset
- MyDayPage hash-scroll rAF + HeroPulse clamp/reset
- ScoreStack 1-signal vs 4-signal rendering + tooltips
- IntentSection reopen + HeroPulse triage advance
- Update HeroNow + MyDayPage tests — button selectors, hidden sections
- Playwright E2E for dark-mode toggle (cycle, persist, system pref)
- TodaysPulseSection displays contiguous ranks #2-#5 even with gappy server ranks
- ThemeToggle unit tests (icon swap + click cycles theme)
- Theme-store unit tests (initial=system, setTheme persists, cycleTheme rotation)
- Tighten TriageSection.test fixture types for build
- Wave 4.1 — Vitest tests for new sections
- Wave 4.2 — Playwright E2E smoke spec
- Mock fetchval for new W1.7-B preconditions in stale tests
- WS-PA-W6 repair Wave-6 quality-gate stragglers
- WS-PA-W4 endpoint integration + L1 unit + frontend regression + feedback-loop E2E + dead-code cleanup
- WS-PA-W1ab repair pre-existing tests broken by lifecycle-redesign deletions
- WS8 fix existing test regressions from B2.3/B2.5/B2.9 component changes
- WS8 update bookmark/archive tests for B1.2 behavior changes
- WS8-B3.7 surface chips + FeedPaperRow booleans + TrashView + BulkToolbar + keyboard shortcuts
- WS8-B3.4 pulse generator excludes archived/dismissed candidates
- WS8-B3.5 digest excludes archived/dismissed; legacy 'starred' status rejected
- WS8-B3.8 Playwright full-lifecycle smoke (Inbox→Save→Library→Star→Archive→Dismiss→Trash→Restore→HardDelete)
- WS8-B3.2 pulse rate_card lifecycle semantics (save→starred+saved+up; dismiss→dismissed+down; open=noop)
- WS8-B3.3 recommender excludes dismissed candidates
- WS6-A4b add encrypted_value=None to FakeRecord fixtures (M5)
- WS6-A4c job-store fixture payload field (L5)
- Repair post-r14 mocked smoke coverage
- Verification helper + confidence SSE event coverage (WS-2.1)
- Use pytest.mark.usefixtures for side-effect-only fixtures
- Search_preview can read for library matching
- Router coverage — projects, tasks, milestones CRUD smoke tests
- Orchestration coverage — author alerts, briefing, deadline warning, review reminder
- Phase E4 — poll library unit tests
- Client, service, and router unit tests
- Characterization tests before ingestion/ split (C3 pre-step)
- Remove sys.modules stubs and redundant sys.path blocks (Wave A1+A2)
- Fix cross-test pollution — limiter / extraction / telegram state
- Restore limiter.enabled in fixture teardown (12 files)
- Fix CORS and snapshot_path expectations
- Fix Wave E batch 2 — LE + misc PI assertion failures
- Fix Wave E batch 1 — assertion/logic failures
- Fix Wave E batch 3 — verification fuzz + telegram pulse delivery
- Scope uvicorn+apscheduler stubs to fixture
- Scope module-level sys.modules stubs to fixtures
- Override verify_api_key in test fixtures (Wave C)
- Scope sys.modules stubs to fixtures (Wave B)
- Enable repo-root pytest — importlib mode + stale file cleanup (A3)
- Lift fitz stub to service-level conftest
- Silence remaining pyright attr-defined on stub-module assignments
- Wave C4 — drop over-mocking in test_script_db + test_embedder
- Fix 6 pyright errors in new Wave C tests
- Wave C1 — ≥1 happy-path + ≥1 edge-case per module
- Wave C3 — extraction_jobs + rate_limiter + eval_pulse smoke
- Wave C2 — models + cards router + jobs router
- Round 11 Wave C — internal_api + settings_internal_reload tests (H-03)
- Add 6 new Playwright specs for jobs + upload + cards + sources + pomodoro + discovery
- Update Playwright specs for My Day + Research Feed + Settings Pulse tab
- Parameterised fatal-error tests for stage1/stage3/assemble/upsert paths
- Integration test for SSE /stream endpoint (noop.test round-trip)
- Update tests for UX Round 3 renames (ProjectPulse→Progress, wizard step renumber, Ask tab restructure)
- Update ResearchFeedPage and ExtractionTemplateSection tests for tab restructure
- Fix incomplete batchProcessPapers mock type
- Update stale assertions for renamed tab + empty state copy
- Scoring formula coverage [CQ-5.2]
- Adversarial SSRF + size/page unit tests [CQ-5.1]
- Setup wizard happy path + skip flow (C1)
- Stream L — 30-paper labeled fixture for eval harness
- Stream K — red tests for pulse rating callbacks
- Stream K — red delivery tests for thin research_pulse
- Stream I — red PulseDeck widget test
- Stream G — graceful-degradation regression tests
- Stream G — discovery fan-out + dedupe red tests


### Db
- Migration 031 — Zotero papers/projects columns + user_config seeds


### Polish
- U6 — dark-mode track contrast on progress bars
- U3+U4 — PulseCard ScoreStack + PulseDeckPage v5 header
- U1 — BulkToolbar floating rounded pill
- Narrow except + safe-strip + async PDF write


### Style
- Ruff auto-fixes after Wave E batches


### Ui
- WS-7 contract + relocate SectionHeader to typography/MarkerCaption
- WS-6 drop redundant CardTitle in Settings sub-components
- WS-5 drop LearningCardsPage SectionHeader markers
- WS-4 demote Projects sub-tab SectionHeader markers to inline meta
- WS-3 drop Citation/Knowledge/Extraction SectionHeader markers
- WS-2 drop PaperDetailPage SectionHeader markers
- WS-1 drop redundant Home/Settings/Feed captions


### Wave-0/0A
- Refresh RUNTIME_REPLAY_VERSIONS to (33 52 53) and wire migration lint into pre-commit


### Wave-0/0C
- Align ci-smoke + setup banner + CORS to HTTP for direct dashboard access


### Wave-0/0D
- Replace inert eslint-disable suppressions with proper ref-pattern fixes


### Wave-1/1A
- Telegram bot 8 fixes (BIDI strip, ack-after-auth, truncate helper, rate-limit start, explicit X-API-Key, marker user_id, /inbox command, qualname rate-limit key)


### Wave-1/1B
- Frontend ~17 fixes (papers-feed invalidation, paper_url scheme guard, IntentSection sub-query isError, a11y labels/htmlFor, ModePicker tablist, drop YesterdaySection, navigate-bridge, SectionHeader shim removal, PartialGenJob, error narrowing, BookOpen dedup, heroMode persist, parseTemplateFields, focus-session aria)


### Wave-1/1C
- PI router/service ~13 fixes (qwen3 import guard, pdf 502 dev gate, noop guard, embed retry, system rate-limits, qwen3 MAX_PASSAGES, SSE helpers DRY, reranker private __all__, trusted proxies refresh, Confidence.NONE, discovery 3-tuple default, sklearn version pickle, pdf_processor dev allowlist)


### Wave-1/1E
- Migrations+DB (mig 061 daily_intent.created_at, fix mig-59 probe + add mig-60/61 probes, $$ state machine for _strip_outer_transaction_control, mig 057 add user.timezone, mig 060 comment header)


### Wave-1/1F
- Procrastinate cutover finishing (aborting->cancelled, BaseException catch, jobs filter strict, _ctx_shim is_error, classifier_job_id surface, InMemoryConnector test, task_registry docstring)


### Wave-1/1G
- Infra (langfuse/n8n/cloudflared _FILE secrets, drop dead OPENAI/ANTHROPIC env, Caddy fail-fast + body cap, CADDY_IMAGE in versions.env, BACKUP_ENCRYPT_KEYFILE, openssl iter 600000, entrypoint password lazy-read, hostname pinning)


### Wave-1/cleanup
- Fix 5 regression-collateral test failures (W1-10 milestone deadline; W1-20 ctx_shim SQL; W1-7 task_registry record_terminal_outcome shape)


### Wave-2/2.1
- Promote paper_user_state helpers to jarvis_common (W2-26: 5-variant on_conflict, repoint 4 call sites)


### Wave-2/2.2
- Invert task_registry dependency direction (W4-1: jarvis_common owns app factory + register_tasks API; services own kind->handler dicts)


### Wave-2/2.3
- App_factory lifespan AsyncExitStack (W1-23: partial startup -> partial teardown; eliminates double-execution bug)


### Wave-3/3.2
- Debug_pulse dev-mode gate (W1-5 reshape: 404 in prod; global queries documented)


### Wave-3/3.3
- Drop unreachable confidence=0.5 branch (W1-24 reshape: whitespace-only-quote -> 0.0)
<!-- generated by git-cliff -->
