# Component Map — what to create, edit, delete

This is the file-by-file plan for landing v5 in the existing JARVIS frontend. Paths assume the standard Next.js layout under `frontend/`.

---

## Create (new files)

### `frontend/components/myday/`
- **`HeroNow.tsx`** — the signature hero card with mode picker. Renders `HeroPulse` / `HeroThread` / `HeroTask` based on internal state; persists last mode in `localStorage`. Reference: `reference/v5-calm-ritual-v2.jsx` § HeroPulse / HeroThread / HeroTask.
- **`HeroPulse.tsx`** — Pulse #1 hero variant.
- **`HeroThread.tsx`** — Resume-thread hero variant.
- **`HeroTask.tsx`** — Continue-task hero variant.
- **`DateMasthead.tsx`** — top date + quote + 4 mini-stat counters.
- **`YesterdaySection.tsx`** — yesterday's recap with carryover links.
- **`IntentSection.tsx`** — single-sentence intent + tasks ladder. Composes existing `<TaskList>`, but the styling is rebuilt — see `TaskList.tsx` notes below.
- **`ProjectsSection.tsx`** — 3 active project rows.
- **`ThreadsSection.tsx`** — 3 open-thread rows.
- **`TriageSection.tsx`** — unified action-items + missing-foundational list. Composes existing `<ActionItemsCard>` row + `<MissingFoundationalCard>` row data.
- **`LearningFocusSection.tsx`** — 2-column learning + focus pair.
- **`EndOfDaySection.tsx`** — 3 reflection prompts.
- **`SectionHeader.tsx`** — the `§ Name · meta` pattern, used by every section.
- **`ScoreStack.tsx`** — 4-stop stacked bar for the score breakdown. Used in HeroPulse and PulseRow.
- **`PulseRow.tsx`** — single row layout for pulse list (rank 2–5 in the section, NOT rank 1 — that's in the hero).
- **`TaskRow.tsx`** — single row in the tasks ladder with project badge + ▶ focus + ✕ delete affordances.

### `frontend/styles/`
- **`design-tokens.css`** — paste from `handoff/design-tokens.css`. Imported in `globals.css`.

---

## Edit (existing files)

| File | Change |
|---|---|
| `MyDayPage.tsx` | **Rewrite.** Becomes a single-column `max-w-[860px] mx-auto px-10 py-10 space-y-12` stack of the new section components. Drops the 4-column grid entirely. Loads `/api/my-day` as before but reads new fields (yesterday, threads). |
| `Sidebar.tsx` (or wherever the topbar lives) | Insert `<HeaderPomodoro>` between the jobs indicator and the keyboard-shortcuts button. Confirm it shows the active task name (truncated to 120px). |
| `HeaderPomodoro.tsx` | Verify it accepts and displays `activeTask?.title`. If not, add a prop. Truncate at `max-w-[120px] truncate`. |
| `TaskList.tsx` (or `TaskRow.tsx` if separate) | Re-skin to match v5 spec — number prefix, ⌃ circle, project badge with `borderColor + color = task.color`, hover ▶ focus button bound to `pomodoroStartWork({task})`, opacity-revealed ✕. Don't change the data/mutation hooks. |
| `LearningCardsSummary.tsx` | Reduce to a card body inside the new `LearningFocusSection`. Keep existing logic — orange CTA when `due > 0`, retention/streak as secondary stats. The wrapping section header moves out into `SectionHeader`. |
| `ProjectPulse.tsx` | This becomes obsolete on My Day (logic absorbed by `ProjectsSection.tsx`); keep the file for `/projects` if it's used there. Otherwise delete after Phase 2. |
| `PulseDeckPage.tsx` (`/pulse` route) | Convert to **filterable archive** view — no longer the canonical scoring surface. Same component logic, but add a date filter and "Today's Pulse" can be removed from its top (since My Day owns it). |
| `globals.css` | `@import` the new `design-tokens.css` and ensure dark-mode `[data-theme="dark"]` override block is wired. |
| `tailwind.config.ts` | Merge in `tailwind.config.snippet.ts` (extends `colors`, adds `fontFamily.serif` and `fontFamily.mono`). |
| `app/layout.tsx` (or `_app.tsx`) | Add `next/font` loaders for Source Serif 4, Inter, JetBrains Mono. Apply class to `<html>` or `<body>`. |

---

## Delete (after Phase 2 lands)

- `MyDayHeader.tsx` (the 4-tile counter row at the top of v0) — replaced by the masthead's right-side stats.
- `ProjectPulse.tsx` (only if not used elsewhere — check usages first).
- The grid-of-cards CSS in `MyDayPage.module.css` if such a file exists.

---

## Untouched (do not modify)

- All `/api/*` route handlers except those listed in `DATA_CONTRACTS.md`.
- `PulseCard.tsx` underlying logic (the new `PulseRow.tsx` is a new layout that consumes the same data shape).
- `ActionItemsCard.tsx` data fetching — only its rendering moves into the new TriageSection layout. Keep its hook.
- `MissingFoundationalCard.tsx` data fetching — same as above.
- `Pomodoro` engine / hook (whatever file owns the timer state). Just slot it into the topbar via `HeaderPomodoro`.
- `FSRS / Card` engine.

---

## Naming convention

- New files use **`PascalCase.tsx`** in `components/myday/`.
- Section components are suffixed with `Section` (e.g. `IntentSection`, `ProjectsSection`).
- Row components are suffixed with `Row` (e.g. `TaskRow`, `PulseRow`).

---

## Order of work (within Phase 1)

Recommended dependency order so you can test as you go:

1. `design-tokens.css` + tailwind config + fonts in layout
2. `SectionHeader` (everything depends on it)
3. `MyDayPage.tsx` rewrite as empty section stubs
4. `DateMasthead` → `YesterdaySection` → ship → check rhythm
5. `HeroNow` (start with Pulse mode only) → `HeroPulse`
6. `IntentSection` + `TaskRow` rebuild
7. `ProjectsSection`
8. `TriageSection` (compose existing data hooks)
9. `LearningFocusSection`
10. `EndOfDaySection`
11. Topbar Pomodoro chip wiring

Phase 2 starts at: `thread` entity → `ThreadsSection` → `HeroThread` → smart hero default algorithm → keyboard nav.
