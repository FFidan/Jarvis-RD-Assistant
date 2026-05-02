# Changelog

All notable changes to JARVIS RD Assistant will be documented in this file.

## [1.5.0] - 2026-05-02 — Contracts Wave 1 (settings cleanup + Pulse tuning + UX polish)

Phase-A continuation per [docs/plans/2026-05-02-contracts-settings-and-ux.md](plans/2026-05-02-contracts-settings-and-ux.md). 6 commits on `feat/contract-impl-wave-1` (`75ffd80..50941c1`) + 2 carryover follow-up commits (`0b732b6` star transition guard, `18457d4` slider fallback). All 4 evergreen contracts at [docs/contracts/](contracts/) refreshed with the now-resolved dispositions for the GHOST/PARTIAL/ANOMALY tail.

### Removed (BREAKING — config-API)
- 5 GHOST `user_config` keys removed from `_ALLOWED_CONFIG_KEYS` in `services/paper_ingestion/paper_ingestion/routers/settings.py`: `paper.max_daily`, `paper.auto_generate_cards`, `ui.page_size`, `ingestion.max_papers_per_run`, `ingestion.chunk_size`. `PUT /api/config/<key>` for any of these now returns HTTP 400 "Unknown config key". Seed UPSERTs removed from `db/init.sql`. Settings UI no longer renders the corresponding controls.

### Added
- `fsrs.learning_steps` is now a real py-fsrs parameter (per-review DB read; default `[1m, 10m]` matches the library default).
- `zotero.auto_push_on_star` toggle wired to the star handler. With the toggle ON, project-linked papers auto-enqueue a `zotero.push` job on the off→on star transition. Idempotent — repeated `/star` calls do not double-enqueue (transition guard at `0b732b6`).
- PulseSection badge now distinguishes `last_error` (red Failed) from `degraded_reason` (amber Degraded with reason tooltip) from healthy (green OK).
- Tooltips on the 4 conditional weight sliders (`classifier`, `citation_pagerank`, `citation_count`, `citation_adamic_adar`) explain the activation gate.

### Changed
- `fsrs.desired_retention` promoted PARTIAL→LIVE: dropped startup cache in `services/learning_engine/learning_engine/main.py`; read per-review.
- Pulse latency tuning: `_LLM_CONCURRENCY = 5 → 8` (`services/paper_ingestion/paper_ingestion/pulse/scoring.py`); `_DEFAULT_STAGE2_TOP_K = 50 → 40` (`services/paper_ingestion/paper_ingestion/pulse/profile.py`). Worst-case 40×120/8 = 600 s — fits inside the existing `_STAGE2_TIMEOUT_SECONDS` cap. Frontend slider fallback synced (`18457d4`).
- Doc rollup: `docs/contracts/01-settings.md` reflects all of the above (commit `50941c1`).

### Fixed
- `zotero.enabled` ANOMALY: deleted orphan read in `services/paper_ingestion/paper_ingestion/scheduler.py`. The legitimate `LIKE 'zotero.%'` wildcard consumer at `services/paper_ingestion/paper_ingestion/services/zotero_service.py` is unaffected.
- `star_paper` off→on transition guard (`0b732b6`): reads `paper_user_state.starred` before upsert; double-/star calls (client retry, double-tap) no longer double-enqueue Zotero pushes.

### Notes
- No DB migrations. Stale `user_config` rows for the 5 removed keys are harmless orphans on existing DBs (no remaining reader).
- Source plan: `docs/plans/2026-05-02-contracts-settings-and-ux.md` (now flagged SHIPPED).
- Branch: `feat/contract-impl-wave-1` — to be merged into master in a later consolidation alongside the parallel UI redesign branch.
- Out of scope (deferred to Phase B): Marathon B.1 Instructor / B.2 Langfuse / B.3 mxbai-rerank-base-v2 / B.4 Taskiq.
- Quality gates at branch tip: ruff clean, frontend lint clean, 6/6 zotero-push tests + 20/20 papers-lifecycle tests + 563/563 frontend tests pass.

## [1.4.4] - 2026-05-02 — Round 15 Wave 1.8 (post-deploy bug fixes + inbox source filter)

W1.7 deployment surfaced 4 issues. Bundled with one feature gap into W1.8: 4 file-disjoint tasks (W1.8-A backend; B+C+D frontend), all merged at 48ce145 on `audit/round-15-w1.8-postdeploy-fixes`.

### Fixed
- **W1.8-A `save_paper` allows `reading` state**: `services/paper_ingestion/paper_ingestion/routers/papers.py` precondition tuple was `("inbox","done","to_read")`, blocking the Library "Set Aside" affordance (reading → to_read) with HTTP 409. Added `"reading"` to the allowed-states tuple. New parametrize case in `tests/test_papers_lifecycle.py`.
- **W1.8-A `load_today` excludes trashed papers from deck**: `services/paper_ingestion/paper_ingestion/pulse/deck.py` SQL had only `WHERE pc.deck_id = $1` — trashed papers came back in the deck response on every reload. Added `AND COALESCE(pus.state, 'inbox') != 'trash'` (the LEFT JOIN `paper_user_state` alias `pus` was already in place). New `tests/test_pulse_deck.py::test_load_today_sql_excludes_trash_in_where_clause`.
- **W1.8-B BulkToolbar Select All label visibility**: the "N selected" span was wrapped in `{selectedIds.size > 0 && (...)}`, so the checkbox had no visible label with zero selections. Always render the `aria-live="polite"` span; show `"Select all"` when empty, switch to `"N selected"` once non-empty.
- **W1.8-C PulseCard Trash & Reject optimistic remove**: `trashAndRejectMut` had no `onMutate` — card stayed visible until the background refetch resolved. Added a cache snapshot + `cards.filter()` patch, moved `invalidateQueries(['pulse-today'])` to `onSettled`. New `PulseCard.test.tsx::trashAndReject removes card from pulse-today cache optimistically`.

### Added
- **W1.8-D Inbox source-type filter chips**: backend `GET /api/papers/feed` already accepted `source_types` (CSV) — only the UI was missing. Added `InboxSourceFilter` type, extended `fetchFeed` with `sourceTypes` param (forwards `source_types=` to backend), threaded `sourceTypes` through `FeedView` props (cache-key-inclusive), rendered 5 source chips on Inbox surface (All sources / arXiv / Semantic Scholar / OpenAlex / PubMed) with URL param `?source=` powering filter state. Library surface unaffected. 3 new tests in `ResearchFeedPage.test.tsx`.

### Notes
- Source plan: `docs/plans/2026-05-02-round-15-w1.8-postdeploy-fixes.md`.
- Branch: `audit/round-15-w1.8-postdeploy-fixes` (merged + deleted).
- No DB migrations. No env-var changes. No spec amendments.
- Quality gates: ruff clean, pyright 0/0/0, frontend lint clean, frontend tsc clean, frontend production build clean, 1345 backend tests + 556 frontend tests pass.
- Out of scope (deferred to Phase B): Marathon B.1 Instructor / B.2 Langfuse / B.3 mxbai-rerank-base-v2 / B.4 Taskiq.

## [1.4.3] - 2026-05-02 — Round 15 Wave 1.7 (W1.6 post-merge bug fixes + Trash UX completion)

Live testing of W1.6 surfaced 10 distinct issues; a 5-agent audit (`docs/plans/2026-05-02-round-15-w1.6-postmerge-bug-audit.md`) traced most to two backend root causes plus 5 independent frontend/operational gaps. Bundled into 6 file-disjoint sub-batches over two waves.

### Fixed
- **W1.7-A FeedPaper serializer (root cause #1)**: `row_to_feed_paper` in `services/paper_ingestion/paper_ingestion/converters.py` never passed `state` or `state_before_trash` from the SQL row to the `FeedPaper` constructor — Pydantic defaulted every paper to `state="inbox"`. This silently broke (a) coloured READING/DONE/TRASH badges (the W1.6-A CSS fix received always-`inbox` data), (b) per-card button rendering on Library AND Trash surfaces (`FeedPaperRow` branches on `state`), and (c) the "NEW" badge that gates on `state === 'inbox'`. One-liner fix: add `state=row.get("state", "inbox") or "inbox"` and `state_before_trash=row.get("state_before_trash")` with defensive `.get(...)` so legacy thin row-builders keep working. New `tests/test_converters.py` (3 cases).
- **W1.7-B `_trash_paper` re-trash idempotency (root cause #2)**: ON CONFLICT DO UPDATE wrote `SET state_before_trash = paper_user_state.state` unconditionally — when the paper was already in `trash`, this writes `'trash'` into a column whose CHECK constraint forbids it (migration 047), so trash-while-trashed returned 500. Fixed via CASE expression that preserves the existing `state_before_trash` on re-trash; otherwise records the current state. Adds `_assert_paper_in_states` multi-state guard helper + state preconditions on `save_paper` (allows inbox/done/to_read — `to_read` retained for Pulse Save→Unsave→Save round-trip), `skip_paper` (inbox only), `reading_paper` (to_read/reading/done). `done_paper`/`trash_paper`/`trash_and_reject_paper`/bulk dispatcher remain unconditional. Stale-cache writes now surface 409 (UI handles via React Query `onError`). 5 new lifecycle tests + 4 stale router tests updated.
- **W1.7-C Pulse mutation hygiene**: PulseCard's `trashAndRejectMut` invalidated the dead key `['pulse-deck']` instead of `['pulse-today']`, so trashed cards stayed visible. PulseDeck and PulsePreviewCard `rateMutation` only updated a local `Set<number>` on success and never invalidated `['pulse-today']` — PulseCard's `isSaved = card.user_state === 'to_read'` (W1.6-C) then stayed false until some unrelated refetch ran, looking like "clicking Save on card A flips bookmarks on cards C/D/E". Replaced with optimistic `onMutate` (gated on `rating === 'save'`, patches `user_state` to `'to_read'`), `onError` rollback, `onSettled` invalidate. 2 new PulseCard tests.
- **W1.7-D ActionItemsCard duplicate "Expand to triage"**: My Day's Action Items rendered TWO chevron-toggle buttons when collapsed — one in the CardHeader and a duplicate in CardContent. Replaced the CardContent collapsed-summary `<div>` with a count-only `<p>`; the header keeps the toggle. Added `title=` to the header "Process all (N)" button explaining why N (only PDF-downloaded papers) can diverge from the total unprocessed count.
- **W1.7-E+F Trash bulk Delete Forever + Select All + copy unification**: Trash surface had no bulk Delete Forever and no Select All; per-row Delete Forever was an icon-only X. Bulk "Save" button read "Save to Library" while per-card tooltip + Library sub-chip read "Reading List" (drift). Added `hard_delete` to `BulkActionRequest.action` Literal + `_apply_bulk_action` arm (precondition `state='trash'`; per-paper SAVEPOINT isolates failures; Qdrant cleanup best-effort, orphan vectors logged). BulkToolbar gains a "Select all on this page" checkbox (allChecked / indeterminate states), `hard_delete` button wrapped in `HardDeleteModal` (typed-confirmation gate); save label unified to "Save to Reading List". HardDeleteModal refactored with discriminated-union props to support both single-paper (existing) and bulk (new) modes — legacy callers unaffected. Per-row trash X promoted to "Delete forever" labelled button. 2 new bulk-delete backend tests + 4 new BulkToolbar tests.
- **W1.7-G PDF text-extraction GPU OOM mislabelled as "embedding error"**: blanket `except (httpx.HTTPStatusError, RuntimeError)` in `pdf_workflow.py` re-raised every failure as "Embedding service error" — but on a 16 GB GPU shared with Marker's Surya layout model, the real failure is `torch.OutOfMemoryError`, not the embedding call (LiteLLM's `embed` works fine, verified via `curl`). Now distinguishes `torch.OutOfMemoryError` → "PDF text-extraction GPU out-of-memory. Lower OLLAMA_MAX_LOADED_MODELS (default 3 → try 2) or set TORCH_DEVICE=cpu" from CUDA-message `RuntimeError` → "PDF text-extraction GPU error" from genuine httpx failures → "Embedding service error". `.env.example` documents the OLLAMA_MAX_LOADED_MODELS / TORCH_DEVICE trade-off near the existing Ollama config. 3 new pdf_workflow tests.

### Notes
- Source plan: `docs/plans/2026-05-02-round-15-w1.7-postmerge-bugfix.md`.
- Branch: `audit/round-15-w1.7-postmerge-bugfix`.
- No DB migrations. No env-var changes (only `.env.example` documentation). No spec amendments (DB columns unchanged; UI renames stay in UI).
- Quality gates: ruff clean, pyright 0/0/0, frontend lint clean, frontend tsc clean, frontend production build clean, 1343 paper_ingestion tests + 543 learning_engine/telegram_bot/jarvis_common tests + 551 frontend tests pass.
- Out of scope (deferred): structural My Day 2-column dashboard redesign (W1.8); "select all matching filter" (vs "select all on page"); Marathon Phase B sprints; audit Round 15 Wave 2 (correctness sweep).

## [1.4.2] - 2026-05-02 — Round 15 Wave 1.6 (UX polish round 2 — bug fixes + design pass)

Live testing of Wave 1.5 surfaced 13 distinct issues across three classes (6 confirmed bugs/regressions, 4 UX/design questions, 3 backend investigations). All bundled into one wave and executed via 9 file-disjoint parallel sub-batches.

### Fixed
- **W1.6-A FeedPaperRow polish**: state badge variant flipped from `secondary` (which diluted the per-state Tailwind colors via `bg-secondary text-secondary-foreground` precedence) to `outline`, so `STATE_BADGE_CLASSES` colors now render correctly. Wrapped the remaining icon+text action buttons (Save / Mark Reading / Mark Done / Set Aside / Re-open) in Radix `<Tooltip>` — UX-B.3 in W1.5 only covered icon-only buttons. Closes A3, A4.
- **W1.6-B FeedbackButtons untoggle lock**: dropped the sticky `&& !clearMutation.isSuccess` gate from `positiveActive`/`negativeActive` — React Query's `isSuccess` stays `true` permanently after the first successful clear, which locked buttons into ghost state forever on subsequent feedback cycles. Synchronous `setLastSignal(null)` before `clearMutation.mutate()` eliminates the one-frame race. New regression test `FeedbackButtons.untoggle.test.tsx` (3 cases). Closes A1.
- **W1.6-C PulseCard Save toggle race**: dropped the `rated &&` gate from `isSaved`; rely solely on server-authoritative `card.user_state === 'to_read'`. Added optimistic `onMutate` to `unsaveMut` (cache snapshot + immediate `user_state='inbox'` patch + `onError` rollback). Eliminates the network-round-trip window where rapid double-click was firing `onRate('save')` twice. Closes A2.
- **W1.6-D Ask streaming preserved across navigation**: moved the `AbortController` from per-hook `useRef` to a module-level `Map<chatId, AbortController>` in `chat-store.ts` (with `registerStream` / `unregisterStream` / `abortAllStreams` helpers). Removed the unmount-cleanup `useEffect` from `use-streaming-chat`. Streams now complete in the background and write into the shared chat-store via the closure; navigating to /analytics mid-stream and back shows the full response. `auth-store.logout` calls `abortAllStreams()` for cleanup. Inverted the D.3 unmount test + added a logout-abort test. Closes A5.
- **W1.6-E Library chip tooltips + label-hover swap**: added tooltip strings for All / Starred / Done chips (only Reading + Reading List had them). Replaced `<InfoTooltip>` `(i)` icons on chips with Radix `<Tooltip>` wrapping the chip label itself, per user decision B2 (`(i)` icons retained on Settings section headers). Closes A6, B2.
- **W1.6-F Analyze Paper smart-mode + structured error UI**: Manual steps section now collapsed behind a "Show advanced ▾" toggle (default closed); each manual step (Download / Process / Generate Summary) renders only when its precondition is unmet (`!pdfDownloaded`, `pdfDownloaded && !hasChunks`, `hasChunks && !hasSummary`); when all stages complete a "All pipeline stages complete" note replaces them. Error banner now displays structured `error_type: error_detail` (when backend supplies them — see W1.6-I) with a per-stage Retry button that calls only the failed stage's mutation. Extended `AnalyzeErrorEvent` type with optional `error_type?: string | null` + `error_detail?: string | null`. Closes B1, partial of C1.
- **W1.6-G Notes UI disambiguation (rename + tooltips)**: sidebar `<h3>My Notes</h3>` → `<h3>Quick Rating <InfoTooltip/></h3>` with explanatory copy ("Per-paper rating, flag, and a one-line note. Saves to your paper state. For longer page-anchored notes use the Annotations tab."); sidebar label `Notes` → `Comment (optional)`, `rows={4}` → `rows={2}`; button `Save Notes` → `Save Rating`; tab label `Notes` → `Annotations` with intro line "Page-anchored highlights and notes (separate from your Quick Rating in the sidebar)." UI strings only — DB columns / API endpoints unchanged. Updated 4 string assertions in `PaperDetailPage.test.tsx`. Closes B3.
- **W1.6-H My Day tactical compact pass**: PomodoroTimer renders a slim row (time + Start button) when idle, full layout when running, with stats hidden behind a `<details>` (saves ~120px). PulsePreviewCard already capped at 3 — verified, no change. ActionItemsCard wrapped in an accordion (collapsed by default if `unprocessed.length > 5`, open if ≤5; user-toggled state respected on subsequent refetches). MissingFoundationalCard returns `null` when empty (was previously rendering a placeholder). Section order changed to: Header → Pulse Preview → Action Items → grid(Pomodoro|Tasks) → MissingFoundational (cond) → grid(Learning|Projects). Saves ~60% page height when no major data exists. Structural 2-column dashboard redesign deferred to W1.7. Closes B4 (tactical scope).
- **W1.6-I Backend structured PDF error + Stage 2 progress granularity**: Analyze SSE error event now emits `{type: 'error', stage: 'process_pdf', error_type: type(exc).__name__, error_detail: str(exc)[:200]}` instead of generic `{message: 'PDF processing failed'}`. The sync `/api/papers/{id}/process` endpoint's `HTTPException.detail` similarly includes structured fields. `pulse/job.py` Stage 2 LLM scoring now emits per-batch progress updates (`stage1_out` processed in batches of 5; each batch calls `update_progress(0.85 + 0.10 * (scored/total), "Stage 2 LLM scoring (N/M)")`, capped at 0.95) instead of a single 0.85 mark for the entire stage. Two new test cases each in `test_analyze.py` and `test_pulse_job.py`. Closes C1, C2.

### Notes
- Source plan: `docs/plans/2026-05-02-round-15-w1.6-ux-polish-round2.md`.
- Branch: `audit/round-15-w1.6-ux-polish-round2`.
- Quality gates: pyright 0/0/0, frontend lint clean, frontend tsc clean, frontend production build clean (W1.5 caught a regression because vitest tsconfig is laxer than `tsc -b`), 540 frontend tests pass, 1328 backend tests pass + 12 skipped.
- Out of scope (deferred): W1.7 frontend-design pass for My Day structural redesign (2-column dashboard, hero "What's next?", drag-to-reorder); Marathon Phase B library integrations (Instructor / Langfuse / mxbai-rerank / Taskiq) remain separate workstreams; audit Wave 2 (correctness sweep) resumes per `docs/plans/2026-05-02-round-15-audit-closeout.md` §6.

## [1.4.0] - 2026-05-02 — Round 15 audit closeout, Wave 1 (production unblockers)

### Fixed
- **W1.1 LiteLLM transparent loopback proxy** — dropped `master_key`, `LITELLM_API_KEY` env wiring, `LITELLM_FALLBACK_ENV_NAMES`, and the `litellm_master_key` Docker secret. Litellm runs loopback-only (`127.0.0.1:4000`) and fronts only Ollama; cloud LLMs (OpenAI/Anthropic/Google) bypass it entirely via encrypted `user_config` keys. `master_key` was a security no-op that introduced a 401 failure mode whenever `LITELLM_API_KEY` and `LITELLM_MASTER_KEY` drifted apart in env wiring. Deleted `litellm/entrypoint.sh` (sole purpose was env injection). `build_litellm_headers()` now always returns `{}`. Reversible if port 4000 is ever exposed beyond loopback. Updates 7 test files + 5 doc files (README, REQUIREMENTS, DEPLOYMENT, PRD, ci-smoke). Closes audit `F-LITELLM-01`.
- **W1.2 Restore precondition guard** (single + bulk) — `PUT /api/papers/{id}/restore` and the bulk-action restore branch both lacked a `state='trash'` precondition; calling restore on inbox/reading/done/already-not-trashed silently demoted papers to inbox. Mirrored the `hard_delete_paper` precedent: `_assert_paper_in_state(state="trash")` before `_restore_paper`. Two new regression tests. Closes audit `H-LC-3`.
- **W1.3 Pulse training: pulse_ratings → recommendation_feedback** — migration 049 dropped `pulse_ratings`; `train_classifier_model` still referenced it and would crash on first invocation. Swap to `recommendation_feedback` with binary `signal` mapping (`'positive'`→1, `'negative'`→0) and `source IN ('pulse_thumbs','dismiss_combined')` predicate. Drops the old 5-state mapping. Closes `C-3`.
- **W1.4 Pulse citation_signals: same swap + log clarity** — `compute_citation_signals` referenced the dropped table; the exception was swallowed by a broad `except` in `pulse/job.py`, silently degrading the pulse deck. Swap to `recommendation_feedback`; preserve the broad-except but emit `exc_info` with `extra={"stage": "citation_signals"}` so future regressions surface in observability. Closes `C-4`.
- **W1.5 Weekly summary: same swap + docstring** — `generate_weekly_summary`'s EXISTS branch referenced the dropped table; would crash whenever any candidate paper exists (not swallowed). Swap to `recommendation_feedback` with same predicate. Update docstring to drop the legacy `paper_user_state.status` reference (Phase A renamed `status`→`state` with different values). Closes `C-5`.

### Notes
- Source plan: `docs/plans/2026-05-02-round-15-audit-closeout.md` (Wave 1 of 5).
- Branch: `audit/round-15-w1-unblockers`.
- Pyright clean (0 errors). 1859 backend tests pass. Live Pulse + L3 feedback verification deferred to PR-time.

## [1.3.0] - 2026-05-01

### Behavior
- **Starred papers remain eligible for re-recommendation** (Sprint 7 B4). Pre-migration-044, `_filter_unread` excluded `status IN ('read','archived','starred')` from the unread candidate pool. Post-migration-044, `archived` is the explicit dismiss signal; starring is treated as a *positive* signal and no longer disqualifies a paper from future ranking. Read and archived papers continue to be filtered out.

### Added
- **Unified Async Job System** (migration 023): `jobs` table, `jarvis_common/jobs.py` module, REST endpoints (`POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/stream` SSE, `POST /api/jobs/{id}/cancel`), frontend Zustand `job-store` + TopNav `JobsIndicator` + Sonner toast notifications. Pulse generate, Paper process, Paper analyze, Generate cards (single + batch) migrated.
- **Global Pomodoro timer** in the TopBar — visible on every page when a session is active, hidden when idle. Click to navigate back to My Day.
- **Source reorder via drag-and-drop**: `paper_sources.display_order` column + `PATCH /api/sources/reorder` endpoint + `@dnd-kit/sortable` grip handle UI.
- **Discover multi-source search**: checkbox multi-select over ArXiv, Semantic Scholar, OpenAlex, PubMed with backend fan-out, dedupe, per-source error isolation.
- **Pulse Diagnostics** endpoint (`GET /api/pulse/debug`) + expandable Diagnostics section in Pulse settings tab.
- Info-bubble tooltips on source cards, notification rows, and Paper Detail action buttons.

#### Phase A — Paper Lifecycle Redesign
- Lifecycle ENUM `paper_user_state.state` (inbox/to_read/reading/done/trash) + orthogonal `starred` BOOLEAN + `state_before_trash` for restore. Migration 047.
- `papers.discovery_origin` ENUM (user_initiated/pulse/recommender/citation_batch) — stamped at insert, immutable. Migration 048.
- `recommendation_feedback` table (replaces dropped `pulse_ratings`): per-paper feedback signals (positive/negative) with source attribution and topic_id for L3 dampening. Migration 049.
- 10 lifecycle endpoints: PUT /api/papers/{id}/save|skip|reading|done|trash|restore|trash_and_reject|star|unstar|annotations + POST /api/papers/{id}/feedback.
- GET /api/papers/feed/counts — 10 named view counts (inbox, library, reading_list, reading, done, starred, trash, active, kept, all_non_trash).
- GET /api/recommendation_feedback (30/min) + DELETE /api/recommendation_feedback?topic_id={id} (5/min).
- L1+L2+L3 backend learning loop: L1 negative_topics/authors → LLM prompt; L2 cosine penalty −λ·cos(candidate, μ⁻) (default λ=0.5); L3 60-day hard exclusion + topic dampening (≥5 negatives in 90d) + min-candidate (<20) safeguard.
- Telegram /inbox command; paper:<action>:<id> callback convention; paper:feedback_(pos|neg):<id>:<source> feedback callbacks.
- Frontend FeedbackButtons component (origin-conditional); RejectedTopicsPanel Settings UI; 5 surface chips + Library sub-chips; state-aware keyboard shortcuts.
- Wave 4: `test_l1_negative_signals.py` (+5 L1 unit tests); `test_papers_router.py` (+18 lifecycle integration tests); `FeedbackButtons.test.tsx` (+3 tests); `ResearchFeedPage.bulk.test.tsx` (+1 H5 direct-setSearchParams test); `feedback-loop.spec.ts` (NEW E2E).
- Wave 7 — contract restoration + gap closure (Wave 6 live smoke surfaced spec drift):
  - **Standalone `/pulse` Pulse Deck page** (new `PulseDeckPage.tsx` wrapping the orphaned `PulseDeck` widget). The previous `/pulse → /my-day` redirect was a legacy artifact from the `?tab=pulse` removal in 2026-04-29. Spec §5.4 Amendment 7 corrects the original "(unchanged)" wording. "View all" link in PulsePreviewCard now resolves correctly.
  - **Paper Detail sidebar feedback section** (FeedbackButtons + optional reason free-text) per spec §5.2 line 349. Buttons moved from PaperHeader to ActionsSidebar; reason textarea slides in after a thumb click and saves via UPSERT. Backend `recommendation_feedback.reason` column was wired since Wave 1cd; only the UI was missing.
  - **Pulse Preview vs full Deck differentiation** per spec §5.2 lines 345-346: `PulseCard` accepts `hideTrashAndReject` prop; My Day Pulse Preview hides 🗑+👎 (top-3 widget shows 👍/👎/💾 only); full `/pulse` Deck shows all four.
  - **Inbox/Pulse Deck overlap** (NEW spec §5.5): Inbox rows with `discovery_origin IN ('pulse','recommender')` render a `✦ Pulse` / `✦ Recommended` badge so the Inbox-firehose vs Pulse-curated overlap is legible.
  - **Global keyboard shortcuts UI**: `KeyboardCheatSheet` moved from feed-scoped to globally mounted in AppShell, opened by a persistent `<Keyboard>` icon button in the TopBar (visible on every authenticated page). Backed by a tiny `useKeyboardShortcuts` Zustand store. The `?` keypress on the Research Feed now dispatches via the same store.
  - **Settings tab URL sync** (`?tab=<value>`) — fixes deep-linking and shareability (Apr 29 audit B5 fix). B6 `/knowledge` redirect and B7 project row click could not be reproduced at HEAD; falsified.
  - **docker-compose `LITELLM_API_KEY` propagation** — app services now authenticate to the litellm proxy. Pre-Phase-A regression that blocked end-to-end Pulse + L3 feedback verification.
  - **Doc cutover catch-up**: PRD.md and REQUIREMENTS.md residual `bookmark`/`is_bookmarked` references replaced with the new state ENUM + starred vocabulary; META plan header refreshed; 3 resolved Apr-29 audit docs archived.
  - **Analytics regression fix** (`pus.status` → `pus.state`/`pus.starred` in 4 files) — surfaced during Wave 6 walkthrough; migration 047 dropped the `status` column but four queries still referenced it.

### Changed
- **My Day redesigned** as a triage dashboard: DayHeader with counters, Pulse preview card (3 papers + link to full deck), Pomodoro + Tasks, ActionItems, Learning summary. Full Pulse deck lives at `/feed?tab=pulse`.
- **Settings** "Recommendations" tab replaced by dedicated "Pulse" tab consolidating enable/schedule/weights/generate/diagnostics. Automation tab no longer carries Pulse controls.
- Sidebar "Feed" → "Research Feed".
- `notifications.timezone` renders as "Timezone" instead of raw dotted key.
- Source cards use uniform tall layout regardless of API-key requirement.

#### Phase A — Paper Lifecycle Redesign
- Lifecycle schema: 5 booleans (saved/dismissed/starred/archived) + status enum → single `state` ENUM + orthogonal `starred` BOOLEAN. Spec §11 atomic cutover — no backwards compatibility.
- Paper-mutation API: /bookmark, /archive, /dismiss, /read, /unsave, /user-state replaced by 10 new lifecycle endpoints.
- Frontend types: `FeedPaper.user_state` changed from boolean axes to `state` ENUM + `state_before_trash` + `starred`.
- Telegram callbacks: pulse_up/down/save → paper:feedback_pos/neg + paper:save; paper_bookmark → paper:save; added paper:trash_reject.
- predicates.py: IS_ARCHIVED_SQL family deleted; replaced by VIEW_PREDICATES (10 surfaces), RECOMMENDER_EXCLUDE_SQL, PULSE_CANDIDATE_EXCLUDE_SQL.
- E2E spec `feed-lifecycle.spec.ts`: removed obsolete LIFECYCLE_WIRED env guard (callbacks were wired in Wave 2.2).

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

#### Phase A — Paper Lifecycle Redesign
- 5 paper-mutation endpoints: PUT /api/papers/{id}/bookmark, /archive, /dismiss, /read, /unsave.
- PUT /api/papers/{id}/user-state (dashboard upsert). Replaced by /annotations + lifecycle endpoints.
- `pulse_ratings` table (dropped in migration 049). Replaced by `recommendation_feedback`.
- `paper_user_state` legacy columns: saved, dismissed, archived, status (text enum), preference.
- IS_ARCHIVED_SQL predicate constants family.
- 2 legacy spec docs: `docs/specs/paper-lifecycle-contract.md`, `docs/specs/feed-information-architecture.md`.
- Dead test helper `make_pulse_rating_row` from conftest.py (Wave 4 cleanup).

## [1.2.8] - 2026-04-29

### WS-AH2 — Verification Audit Fixes

9 commits (c6cfd14..6adef70). Closes 11 findings from the 2026-04-30 verification audit (`docs/plans/2026-04-30-verification-audit.md` + `docs/plans/2026-04-29-ws-ah-plan.md`). Pyright 0/0/0; backend 1762 tests pass; frontend 457 tests pass.

#### Fixed

- **H2 — Hard-delete reorder**: PG commit before Qdrant delete so vectors are not orphaned on PG failure.
- **H5 — Bulk clear on URL change**: bulk selection now clears automatically when the active surface chip changes.
- **H6 / DRY-1 — Archived predicate substitution**: `archived` filter predicate centralised in `query/predicates.py`; all call sites updated to use the shared constant.
- **M8 — Title trim**: paper title is stripped of leading/trailing whitespace on ingest and upsert.
- **M11 / NI-2 — `db_user_id` parameter**: `_simple_digest` scoping corrected; `db_user_id` now threaded through Telegram digest helpers instead of relying on closure capture.
- **NI-1 — `pulse_decks.user_id` column**: deck INSERT now includes `user_id`; rows no longer accumulate with `user_id=NULL`.
- **NI-3 — `HardDeleteModal` onError toast**: mutation `onError` callback added so deletion failures surface a toast instead of failing silently.
- **NI-4 — Top-of-module imports**: deferred imports in `app_factory.py` and related modules hoisted to module level.
- **NI-5 — `app_factory` equal-length contract**: lifespan init and teardown lists verified to have equal length; assertion added to catch future mismatches.
- **NI-6 — Migration lint cwd anchor**: `scripts/check_migration_lint.sh` now anchors to the repo root regardless of invocation directory.
- **L12 — Closed**: confirmed resolved as part of WS-AH1 work; no additional action needed.

---

## [1.2.7] - 2026-04-29

### WS-AH / WS8 — Paper Lifecycle Triage

28 commits (570aac6..4b3d1e1). Introduces a full paper lifecycle triage system (Inbox → Save → Library → Star → Archive → Dismiss → Trash → HardDelete) and closes findings from the 2026-04-29 deep audit. Migration 046 (`paper_lifecycle_triage`) is the schema anchor.

#### FEATURE

- **Migration 046 — paper lifecycle axes**: `saved`, `dismissed`, `updated_at` columns added to `paper_user_state`; `status` enum extended; backfill applied.
- **Feed surface chips**: Research Feed gains Inbox / Library / Search / Ask / Trash surface chips with sub-chip filters; Pulse removed from feed tabs (redirected to `/my-day`).
- **FeedView component**: replaces LibraryTab + NewTab with a single surface-aware `FeedView` component and `useFeedCounts` hook.
- **Paper lifecycle endpoints**: `POST /api/papers/{id}/save`, `POST /api/papers/{id}/unsave`, `POST /api/papers/{id}/dismiss`, `POST /api/papers/{id}/restore`, `DELETE /api/papers/{id}` (hard-delete), `POST /api/papers/bulk`, `GET /api/feed/counts` added to papers router.
- **TrashView + HardDeleteModal**: 2-step title-confirm hard-delete UI; Zotero sync coming-soon placeholder.
- **PaperDetail action bar**: Save / Mark Read / Archive / Dismiss / HardDelete actions wired to mutations.
- **Keyboard shortcuts**: `j/k/s/S/e/d/r/o/Enter/?/Esc` shortcuts via `useFeedKeyboardShortcuts` hook; `KeyboardCheatSheet` modal.
- **CountsBadge**: reactive count badge next to surface chips.
- **Bulk actions**: per-surface bulk select with checkbox, Ctrl+A shortcut, and `BULK-TXN-001` nested savepoints.
- **Telegram**: `/inbox` command; Save / Dismiss inline callbacks; digest excludes archived and dismissed papers.

#### RELIABILITY

- **BULK-TXN-001**: each paper in a bulk action wrapped in a nested savepoint so one failure does not abort the full batch.
- **Pulse lifecycle semantics**: `rate_card` Save → `starred+saved+pulse_up`; Dismiss → `dismissed+pulse_down`; `open` rating is a no-op (early return).
- **Pulse generator**: excludes archived and dismissed candidates from overnight scoring.
- **Recommender**: excludes dismissed (Trash) candidates from recommendations.
- **SSE helper**: `sse_event()` helper + `SSE_DONE` constant extracted; all SSE routers updated.
- **Multi-tenant readiness**: `user_id` bound in all user-state filter queries (`reco`, `pulse`, `weekly_summary`).
- **focus-session ON CONFLICT**: learning engine `ON CONFLICT (paper_id, user_id)` fix + live-PG regression test.

#### DOCS

- **WS-AH1 audit-hotfix-sprint plan**: falsification record for findings that proved not to be bugs added to `docs/plans/2026-04-29-ws-ah-plan.md`.
- **Migration 046 lint**: `scripts/check_migration_lint.sh` scope extended to cover 044+.

---

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
- Settings page for topics, sources, a