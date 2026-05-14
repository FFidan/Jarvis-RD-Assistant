# Implementation prompt — v5 (Calm Ritual v2) of JARVIS My Day

You are implementing a redesign of the `/my-day` route in the JARVIS research-OS frontend. The full design spec, screenshots, and reference JSX are in this folder.

## Your inputs

- **`SPEC.md`** — complete design spec; read this first
- **`COMPONENT_MAP.md`** — file-by-file changes (create / edit / delete) in the existing codebase
- **`DATA_CONTRACTS.md`** — backend entities and API fields you'll need to add or extend
- **`design-tokens.css`** — drop-in CSS variables (paper background, ink-blue accent, type stack)
- **`tailwind.config.snippet.ts`** — Tailwind theme additions
- **`reference/`** — the prototype JSX that produced the screenshots (this is the source of truth when the spec is ambiguous)
- **`screenshots/`** — every state of the design

## What you're building

Replace the current grid-of-cards `MyDayPage.tsx` with a **single-column ritual layout** scaffolded as 9 sections (`§ Yesterday`, `§ Now`, `§ Today's intent`, `§ Projects`, `§ Open threads`, `§ Today's pulse`, `§ Triage`, `§ Learning & focus`, `§ End of day`). The shell (sidebar + topbar) gains a topbar Pomodoro chip showing the active task name; the sidebar order is unchanged.

## Phasing — ship in two waves

### Phase 1 — Visual system + layout (~3–5 days)

Goal: the page looks like v5, even if some interactions are stubbed.

1. Add the type stack: Source Serif 4, Inter, JetBrains Mono via `next/font` or `<link>`. Paste `design-tokens.css` into globals; merge `tailwind.config.snippet.ts` into the project's tailwind config.
2. Build the page scaffold: `MyDayPage.tsx` becomes a single-column max-w-[860px] mx-auto px-10 py-10 stack of `<Section>` children. Background: `bg-[var(--surface-paper)]`.
3. Build the **HeroNow** component with mode picker (Pulse / Thread / Task) — default Pulse, persist choice in `localStorage('myday.heroMode')`.
4. Build the **Yesterday** masthead + carryover list. Backend can return placeholder data — see `DATA_CONTRACTS.md` for the shape.
5. Restore **TaskList** with full per-task affordances (project badge with color + link to `/projects?projectId=X`, hover ▶ Focus button bound to `pomodoroStartWork({task})`, completed-today expandable footer).
6. Add the **§ Projects** block (top 3 active by recency, progress bar + milestone + due).
7. Re-skin existing PulseCard / ActionItemCard / MissingFoundationalCard rows to the v5 visual language (hairline borders, paper-cream surface, mono metadata) — do **not** rewrite their data plumbing.
8. Build the **§ End of day** reflection prompts (3 dashed-underline inputs) — POST to a new `/journal/today` endpoint or stub locally.
9. Update **Sidebar.tsx** topbar: add the `HeaderPomodoro` chip rendering (it already exists as a component — just slot it in if it isn't there yet).

After Phase 1, the page is shippable. The remaining items are nice-to-have.

### Phase 2 — Threads, smart hero defaults, polish (~2–3 days)

1. Add the `thread` entity + API (see `DATA_CONTRACTS.md`).
2. Build the **§ Open threads** section + wire the "Resume thread" hero mode.
3. Implement smart hero defaulting: on first load of the day, pick the mode using a tiebreak (active interrupted Pomodoro → Thread @ >70% progress → top Pulse #1). Cache the user's last manual override in localStorage and prefer it within the session.
4. Add keyboard nav: `j`/`k` jump between sections (use anchor IDs already in the markup), `?` opens shortcuts dialog.
5. Add the **"Process all (N)"** triage action — calls `POST /papers/process_batch`.
6. Move the standalone Pulse Deck route (`/pulse`) into a filterable archive view (different filename in nav: "Pulse Deck"). The canonical scoring surface is now My Day.

## Hard rules

- **No new icon set.** Use the lucide-react icons already in the codebase. The reference JSX uses inline SVGs only because the prototype doesn't have lucide.
- **Preserve every existing API contract.** Don't rename existing endpoints; extend `/my-day` with new fields rather than reshaping it.
- **Do not delete `MissingFoundationalCard`, `ActionItemsCard`, or `LearningCardsSummary`.** Re-skin them and reuse their props. Only `MyDayPage.tsx` is rewritten.
- **Match measurements from the spec exactly.** The visual system (max-width, vertical rhythm, type sizes) is load-bearing — small drifts compound.
- **Keep dark mode working.** `design-tokens.css` includes both palettes; build with `[data-theme="dark"]` overrides from day one.

## What success looks like

The screenshots in `screenshots/` are the acceptance bar. Render the page in the codebase, take the same screenshots, and they should match within a few pixels.

If anything in the spec disagrees with the reference JSX, the JSX wins.
