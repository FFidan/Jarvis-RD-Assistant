# JARVIS My Day · v5 (Calm Ritual v2) — Implementation Handoff

This package contains everything you need to implement the **v5 redesign** of the My Day page (and the supporting shell changes) in the JARVIS codebase.

---

## What you're shipping

The current `MyDayPage.tsx` is a 4-column grid of cards (Pulse preview · Tasks · Action Items · Missing Foundational + Project Pulse + Pomodoro + Cards summary). It works, but it doesn't tell the user where to start, duplicates the Pulse Deck, and reads as a dashboard rather than a working surface.

**v5** rebuilds it as a **single-column ritual flow** with a hero "Now" card. The day has a shape: Yesterday → Now → Intent → Projects → Threads → Pulse → Triage → Learning & Focus → End of Day. Every existing entity is preserved; the visual system warms up (paper-cream background, Source Serif 4 paper titles, ink-blue `#0b3a8a` accent); two new entities are added (`thread`, `journal_entry`).

The shell (sidebar + topbar) gets small but real upgrades: the topbar gains a live Pomodoro chip with the active task name, the sidebar order is unchanged.

---

## How to read this package

Open in this order:

| # | File | Purpose |
|---|---|---|
| 1 | **`IMPLEMENTATION_PROMPT.md`** | The prompt to feed your coding agent. Start here. |
| 2 | **`SPEC.md`** | The complete v5 design spec — every section, every interaction, every measurement. |
| 3 | **`COMPONENT_MAP.md`** | What to create, edit, delete, and which existing files each section touches. |
| 4 | **`DATA_CONTRACTS.md`** | New entities, new API fields, migration notes. |
| 5 | **`design-tokens.css`** + **`tailwind.config.snippet.ts`** | Drop-in token files — paste into the codebase. |
| 6 | **`reference/`** | The actual JSX/JS that renders v5 in the prototype, lifted verbatim. |
| 7 | **`screenshots/`** | Visual reference for every state. |

---

## Screenshots index

The page is captured top-to-bottom as section views (each ~540px tall) so you can review individual states. View `handoff/screenshots/` and scroll through in numerical order.

- `01-above-the-fold.png` — date masthead + § Yesterday + hero (Pulse mode) — the first-load impression
- `02-hero-pulse.png` — hero in default Pulse mode (full hero card visible)
- `03-hero-thread.png` — hero in "Resume thread" mode
- `04-hero-task.png` — hero in "Continue task" mode
- `05-section-intent.png` — § Today's Intent + Tasks ladder (with project badges visible)
- `06-section-projects.png` — § Projects block (3 active rows)
- `07-section-pulse.png` — § Today's Pulse list (rank 2–5)
- `08-section-triage.png` — § Triage row (action items + foundational gaps unified)
- `09-section-learning-focus.png` — § Learning & Focus pair (orange CTA visible)
- `10-section-eod.png` — End of Day reflection prompts
- `11-shell-topbar.png` — sidebar nav + topbar shell context
- `12-completed-expanded.png` — Tasks ladder with "done today" footer expanded

---

## What to do next

1. Have your coding agent read **`IMPLEMENTATION_PROMPT.md`**.
2. Provide it with this entire folder as context.
3. Ship in two phases (the prompt explains phasing).

If anything in the spec is ambiguous, the prototype source under `reference/` is the source of truth — it's the exact code that produced the screenshots.
