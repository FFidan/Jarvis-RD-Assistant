# My Day Redesign — Implementation Decisions

**Status:** RATIFIED 2026-05-02
**Date:** 2026-05-02
**Scope:** Phase 1a (frontend re-skin) + 1b (backend derived fields) + 2 (low-cost entities)
**Authority:** Where this document disagrees with the [handoff/](../../handoff/) package (`SPEC.md`, `COMPONENT_MAP.md`, `DATA_CONTRACTS.md`, `IMPLEMENTATION_PROMPT.md`), **THIS DOCUMENT WINS**.
**Source design preserved:** `handoff/SPEC.md` (visual system) and `handoff/screenshots/` (acceptance bar) remain authoritative for what the page should look like.

---

## 1. Why this overlay exists

The `handoff/` package (Claude Design output, 2026-05-02) proposes a full v5 redesign of `/my-day` with two new entities (`threads`, `journal_entries`), a server-side hero hint, and a 3-prompt journal. After deep reasoning from a research scientist's POV in this session (transcript 2026-05-02), three changes are made before implementation begins. This document captures them so future agents — including this same session after compaction — don't revert to the original handoff verbatim.

The visual system, layout grammar, and acceptance bar (the screenshots) are **not** changed. Only the entity model, the journal default, the hero-hint location, and the path layout are overridden.

---

## 2. Phasing

| Phase | Scope | Backend? | Migrations | Ship-shaped on its own |
|---|---|---|---|---|
| **1a** | Visual system + layout: design tokens, fonts, `MyDayPage.tsx` rewrite as 9-section single-column, all section components, hero with 2 modes (Pulse + Continue task — Resume reading deferred to Phase 2 once we decide how to source it). | None | None | YES |
| **1b** | Extend `/api/my-day` response with derived `yesterday` + `completed_today` (computed from existing `tasks.completed_at` + `pomodoro_sessions`). No schema changes. | ~½ day | None | YES (after 1a) |
| **2** | Migrations 050 (`tasks.color`, `projects.color` / `next_milestone` / `next_milestone_due`) + 051 (`journal_entries`); single-prompt journal section; `POST /papers/process_batch` endpoint; project color badges; "Resume reading" hero mode wired against existing `state='reading'` papers + Pomodoro session history. | ~1 day | 050 + 051 | YES (after 1b) |

Each phase is independently shippable. The user may stop after 1a and live with the new visual system before deciding whether 1b/2 are still desired.

---

## 3. Cuts from `handoff/`

### Cut 3.1 — No `threads` table

`handoff/DATA_CONTRACTS.md` proposes a `threads` entity with manually-maintained `progress: real` and `anchor: text` fields, plus a new `GET /threads/recent` endpoint and a `POST /threads/:id/touch` endpoint.

**Reason for cut: tagging fatigue.** Researchers will not keep `progress` and `anchor` updated as they read. Fields that require conscious user input on every interaction stop being filled in within ~2 weeks; from then on the field shows stale data, which is worse than no data. We've seen this pattern repeatedly in note-taking and project-management tools.

**Replacement:** A "Resume reading" hero mode that derives the same UX from existing data, with zero new fields and zero maintenance:

- **Source paper:** `papers WHERE state = 'reading' ORDER BY (latest pomodoro_session.ended_at where session.task → paper) DESC LIMIT 1`. The `state='reading'` column already exists (shipped in Phase A migration 047 / Wave 1.7 W1.7-A). The task → paper mapping is whatever convention we already use (verify in implementation plan).
- **Section anchor:** optionally pulled from the latest entry in the existing PaperDetail `Annotations` tab, if any. Fallback: omit anchor entirely.
- **Progress:** drop the field entirely. The granularity of "reading vs done" is enough for a launchpad — sub-paper progress was always going to rot.
- **`last_touched_at`:** derived from the most recent Pomodoro session linked to a task that links to this paper, OR from the `papers.updated_at` column.

The hero mode picker stays 3 tabs (Pulse / Resume reading / Continue task) — only the data source changes.

### Cut 3.2 — Journal: one prompt by default, two opt-in

`handoff/SPEC.md` §11 ships 3 reflection prompts always-visible:
1. "One thing that worked"
2. "What's still blocking me"
3. "First move tomorrow"

**Reason for trim: guilt-UI risk.** An always-visible 3-prompt form that users fill in for a week, then skip a day, becomes a daily reminder of a habit they're failing. Empty form → guilt → page abandonment.

**Decision:** Ship with **one prompt visible by default** — "First move tomorrow", because it answers "what should I do first?" for tomorrow's launch (highest leverage of the three). The other two are accessible via a small `+ add reflection` affordance below the visible prompt. Telemetry should track expand-rate on the affordance; if users expand the other prompts on >50% of days, surface them by default in a follow-up.

The `journal_entries` table schema in `handoff/DATA_CONTRACTS.md` stays as-is — it stores `prompts` as a JSONB column, so 1, 2, or 3 keys are equally valid. Schema doesn't care.

### Cut 3.3 — No `hero_default` server hint

`handoff/DATA_CONTRACTS.md` §"Hero default algorithm — server hint" proposes a server-side rule that sets `mode: "pulse" | "thread" | "task"` on the `/api/my-day` response.

**Reason for cut: over-engineering.** The rule is 5 lines of client logic, and the data it needs (active Pomodoro state, last-touched-reading paper, deck readiness) is already on the client at render time via existing TanStack Query keys. Moving it server-side adds a round trip, a backend code path to test, and a synchronization headache when the server's view of "interrupted Pomodoro yesterday" lags the client's.

**Decision:** Implement the rule client-side in `HeroNow.tsx`:

```ts
function pickInitialHeroMode(deck, pomodoro, readingPapers): HeroMode {
  if (pomodoro.lastInterruptedYesterday && pomodoro.remainingMin < 30) return 'task';
  if (readingPapers.length > 0 && hoursSince(readingPapers[0].lastTouched) < 24) return 'resume';
  return 'pulse';
}
```

Cache the user's manual override in `localStorage('myday.heroMode')` — the handoff already specifies that storage key, no change.

---

## 4. Path translation (Next.js → Vite)

The handoff package was written assuming Next.js project layout. Our frontend is Vite + React 19. Translations:

| Handoff path | Our path |
|---|---|
| `frontend/components/myday/` | `frontend/src/components/my-day/` (note hyphen — matches existing convention in this repo) |
| `frontend/styles/design-tokens.css` | `frontend/src/styles/design-tokens.css` (or merge into `frontend/src/index.css`) |
| `app/layout.tsx` (`next/font` loaders) | `frontend/index.html` `<link>` tags to Google Fonts (or self-host woff2 in `frontend/public/fonts/`) |
| `tailwind.config.ts` | `frontend/tailwind.config.ts` (same shape, project root is `frontend/`) |
| `globals.css` | `frontend/src/index.css` |
| `_app.tsx` | `frontend/src/main.tsx` |

---

## 5. Component composition (Phase 1a)

The `handoff/COMPONENT_MAP.md` "Create" list translates 1:1 to our layout. New components live at `frontend/src/components/my-day/sections/` (a new sub-folder so the existing `my-day/` components stay flat at the root):

- `DateMasthead.tsx`
- `YesterdaySection.tsx` (renders empty/skeleton until Phase 1b wires real data)
- `HeroNow.tsx` (mode picker; renders one of the three Hero variants)
- `HeroPulse.tsx`
- `HeroResumeReading.tsx` (replaces handoff's `HeroThread.tsx`)
- `HeroTask.tsx`
- `IntentSection.tsx`
- `ProjectsSection.tsx`
- `TriageSection.tsx` (composes existing `ActionItemsCard` + `MissingFoundationalCard` data hooks)
- `LearningFocusSection.tsx`
- `EndOfDaySection.tsx` (one prompt visible; 2 opt-in via affordance)
- `SectionHeader.tsx` (the `§ Name · meta` pattern)
- `ScoreStack.tsx` (4-stop stacked bar)
- `PulseRow.tsx` (ranks 2–5; rank 1 lives in `HeroPulse`)
- `TaskRow.tsx` (with project-color badge, hover ▶ Focus, completed-today footer)

**Existing components reused without rewrite (data hooks preserved):**

- [frontend/src/components/my-day/ActionItemsCard.tsx](../../frontend/src/components/my-day/ActionItemsCard.tsx)
- [frontend/src/components/my-day/MissingFoundationalCard.tsx](../../frontend/src/components/my-day/MissingFoundationalCard.tsx)
- [frontend/src/components/my-day/LearningCardsSummary.tsx](../../frontend/src/components/my-day/LearningCardsSummary.tsx)
- [frontend/src/components/my-day/PomodoroTimer.tsx](../../frontend/src/components/my-day/PomodoroTimer.tsx)
- [frontend/src/components/my-day/PulsePreviewCard.tsx](../../frontend/src/components/my-day/PulsePreviewCard.tsx) — re-skinned into `PulseRow.tsx` for ranks 2–5; rank 1 lives in `HeroPulse`. The data hook (TanStack Query against `['pulse-today']`) is preserved verbatim.
- [frontend/src/components/layout/HeaderPomodoro.tsx](../../frontend/src/components/layout/HeaderPomodoro.tsx) (already in topbar; verify it shows the active task title and add the `max-w-[120px] truncate` if missing)

**Re-skinned with V5 visual language but same data hooks:**

- [frontend/src/components/my-day/TaskList.tsx](../../frontend/src/components/my-day/TaskList.tsx) — rebuilt as `TaskRow.tsx` with project-color badge, hover ▶ Focus button (binds to `pomodoroStartWork({task})`), completed-today expandable footer.

**Deleted after Phase 1a lands:**

- [frontend/src/components/my-day/DayHeader.tsx](../../frontend/src/components/my-day/DayHeader.tsx) — the old 4-tile counter row replaced by `DateMasthead.tsx` + the right-side mini-stats.

**Render-artifact note:** Where the prototype JSX in `handoff/reference/` has overflow / button-shape issues (visible in some screenshots per user inspection), the implementation MUST use Shadcn primitives (`Button`, `Card`, `Tooltip`, `Tabs`) rather than literal-translating the prototype's hand-rolled inline-SVG + Tailwind. The `handoff/SPEC.md` measurements are load-bearing; the prototype's specific markup is not.

---

## 6. Migration number reservations

- Phase 2 of this redesign reserves migrations **050** (`tasks.color` + `projects.color` + `projects.next_milestone` + `projects.next_milestone_due`) and **051** (`journal_entries` table).
- Marathon Phase B.1 + B.2 (per [docs/specs/2026-05-02-instructor-langfuse-integration.md](2026-05-02-instructor-langfuse-integration.md) DRAFT) does **NOT** introduce migrations — confirmed by reading that spec's §1.1 Goals, §1.2 non-goals, and §2 Architectural Choke Point. All B.1+B.2 work is library swaps + observability decorators on existing call paths.
- Future workstreams should consult this section before claiming a migration number.

Highest existing migration is `db/migrations/049_recommendation_feedback.sql`. Next free is 050.

---

## 7. File-scope coordination with Marathon Phase B.1 + B.2

The redesign and the Phase B.1 + B.2 spec touch zero shared files:

| Workstream | Owns |
|---|---|
| Redesign Phase 1a | `frontend/src/pages/MyDayPage.tsx`, `frontend/src/components/my-day/**`, `frontend/src/styles/`, `frontend/tailwind.config.ts`, `frontend/index.html`, `frontend/src/index.css` |
| Redesign Phase 1b | + the router that defines `GET /api/my-day` (verify path in implementation plan — likely under `services/paper_ingestion/paper_ingestion/routers/`) |
| Redesign Phase 2 | + `db/migrations/050_*.sql` + `db/migrations/051_*.sql` + a journal endpoint module + `POST /papers/process_batch` handler |
| Marathon B.1 + B.2 | `libs/jarvis_common/jarvis_common/llm_client.py` + 6 LLM call sites (`pulse/scoring.py`, `extraction/core.py`, `extraction/entities.py`, `services/contradictions.py`, `learning_engine/card_generator.py`, plus 1 in pulse/) + `pyproject.toml` / `requirements.txt` for the `instructor` + `langfuse` packages + `docker-compose.yml` (Langfuse profile) |

The two workstreams can execute concurrently. The only soft coupling is `pyproject.toml` if both need to add packages: Phase B.1 + B.2 does the heavy adds (`instructor`, `langfuse`); redesign Phase 1a needs no new Python packages; redesign Phase 2 likely needs none either (journal endpoint can use existing `asyncpg` + `pydantic`).

---

## 8. Acceptance criteria — Phase 1a "shippable"

1. `frontend/src/pages/MyDayPage.tsx` renders 9 sections in single-column max-w-page layout (`max-w-[860px] mx-auto px-10 py-10 space-y-12`).
2. `Inter` (body), `Source Serif 4` (paper titles + masthead), `JetBrains Mono` (section markers + metadata) all loaded and visible in browser.
3. Background `bg-[var(--surface-paper)]` applied; ink-blue `#0b3a8a` accent visible on hover/active states (links, hero CTA).
4. Hero defaults to Pulse mode; mode picker switches to "Continue task"; the "Resume reading" tab is present but disabled with a small "(Phase 2)" hint until Phase 2 wires it.
5. Yesterday + Triage sections render with empty/skeleton states when their data isn't yet wired (Phase 1b).
6. EndOfDay section is present but hidden behind a "+ daily journal" affordance until Phase 2 wires the `journal_entries` table.
7. Existing data still renders correctly: Pulse Preview, Tasks, Action Items, Missing Foundational, Learning Cards, Project Pulse — all reachable through the new section structure.
8. Dark mode works: `[data-theme="dark"]` overrides from `design-tokens.css` all render correctly.
9. Quality gates pass: `npm --prefix frontend run lint`, `npm --prefix frontend run test -- --run`, `npx --prefix frontend tsc --noEmit`, `npm --prefix frontend run build`.
10. Visual: matches `handoff/screenshots/01-above-the-fold.png` and `handoff/screenshots/05-section-intent.png` within a few px (modulo content differences from real data vs. prototype mock data).

---

## 9. Deferred / out-of-scope

- **PulseDeck route conversion to "filterable archive."** `handoff/COMPONENT_MAP.md` proposes converting the standalone `/pulse` route into a date-filterable archive. This breaks user habits (the route has its own bookmark value) and is independent of My Day. Decide separately after Phase 1a lands and we see whether My Day's hero owns the "today's pulse" surface fully.
- **Smart hero default algorithm.** Phase 1a defaults to Pulse mode always. The smart-pick rule (interrupted Pomodoro → Resume Reading → Pulse #1) lands in Phase 2 once we have the Resume Reading data source.
- **Keyboard `j`/`k` section navigation.** Nice power-user feature but not load-bearing. Phase 2.
- **Telemetry events.** `handoff/DATA_CONTRACTS.md` lists 6 telemetry events. Decide separately whether we have a telemetry pipeline; if not, this is a separate workstream.

---

## 10. Verified Identifiers

| Citation | File:line | Behavior |
|---|---|---|
| `handoff/SPEC.md` | [handoff/SPEC.md](../../handoff/SPEC.md) (306 lines) | Full v5 design spec — type stack, color tokens (`--surface-paper`, `--ink-blue=#0b3a8a`, etc.), 12 sections from masthead through EOD. §11 specifies 3 reflection prompts. |
| `handoff/DATA_CONTRACTS.md` | [handoff/DATA_CONTRACTS.md](../../handoff/DATA_CONTRACTS.md) (226 lines) | Defines `thread` entity (with manual `progress: real` and `anchor: text`), `journal_entry` (one per user per date with 3-prompt JSONB), adds `tasks.color` + `projects.color/milestone/due`, `POST /papers/process_batch`, `GET /threads/recent`, server-side `hero_default` hint on `/my-day`. |
| `handoff/COMPONENT_MAP.md` | [handoff/COMPONENT_MAP.md](../../handoff/COMPONENT_MAP.md) (92 lines) | File-by-file create/edit/delete plan. Assumes `frontend/components/myday/` (Next.js, no hyphen). |
| `handoff/IMPLEMENTATION_PROMPT.md` | [handoff/IMPLEMENTATION_PROMPT.md](../../handoff/IMPLEMENTATION_PROMPT.md) (58 lines) | Agent prompt; references `next/font`, `app/layout.tsx`, `_app.tsx`. Phase 1 = visual + layout (3-5 days); Phase 2 = threads, smart hero, polish (2-3 days). |
| Marathon B.1+B.2 spec (DRAFT, untracked) | [docs/specs/2026-05-02-instructor-langfuse-integration.md](2026-05-02-instructor-langfuse-integration.md) (467 lines) | "Status: DRAFT (awaiting user review). Scope: Marathon Phase B.1 (Instructor) + B.2 (Langfuse) combined." §2 Architectural Choke Point lists `request_chat_completion_content` (kept), `call_llm` (deleted), `call_llm_json_value` (deleted), replaced by new `call_llm_structured`. Confirmed no migrations introduced. |
| Current `MyDayPage.tsx` | [frontend/src/pages/MyDayPage.tsx](../../frontend/src/pages/MyDayPage.tsx) (71 lines) | The 4-column grid: `<DayHeader>` + `<PulsePreviewCard>` + `<ActionItemsCard>` + grid(`<PomodoroTimer>`, Tasks card with `<QuickAddTask>`+`<TaskList>`) + `<MissingFoundationalCard>` + grid(`<LearningCardsSummary>`, `<ProjectPulse>`). `max-w-4xl`. |
| `frontend/src/components/my-day/` contents | [frontend/src/components/my-day/](../../frontend/src/components/my-day/) | 10 components: `ActionItemsCard`, `DayHeader`, `LearningCardsSummary`, `MissingFoundationalCard`, `PomodoroTimer`, `ProjectPulse`, `PulseDeck`, `PulsePreviewCard`, `QuickAddTask`, `TaskList`. Uses HYPHEN (`my-day`), not handoff's `myday`. |
| `HeaderPomodoro` | [frontend/src/components/layout/HeaderPomodoro.tsx](../../frontend/src/components/layout/HeaderPomodoro.tsx) | Topbar Pomodoro chip — already exists. Verify it shows the active task title; add `max-w-[120px] truncate` to the title span if missing. |
| Highest migration | [db/migrations/049_recommendation_feedback.sql](../../db/migrations/049_recommendation_feedback.sql) | Highest migration number is 049. Next free = 050. Redesign Phase 2 reserves 050 + 051. |
| Phase A `paper_user_state.state` collapse | [db/migrations/047_paper_user_state_collapse.sql](../../db/migrations/047_paper_user_state_collapse.sql) | Migration that introduced the single `state` ENUM with values including `'reading'`. Source of the `state='reading'` query that powers Resume Reading mode. |
