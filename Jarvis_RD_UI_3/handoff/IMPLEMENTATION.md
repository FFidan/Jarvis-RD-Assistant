# Jarvis-RD_Assistant — Bold Direction Implementation Spec

> Hand-off package for the implementation agent. Read this end-to-end before touching code.
> Reference designs: `Redesigns.html` (Bold-direction artboards).
> Tokens: `handoff/tokens.css`. Source JSX prototypes: `redesigns/*.jsx`.

---

## 0 · The one-line brief
Rebuild Jarvis's UI around three verbs (**Today / Read / Reflect**), a slim ⌘K-everywhere
topbar, and three-pane layouts on the high-density screens (Feed, Paper, Projects).
Visual language is "research log / lab notebook": cream paper, serif H1s, § marker
captions, mono numerics. Keep the existing backend lifecycle states intact.

---

## 1 · Non-negotiable constraints

1. **Lifecycle states are backend-wired and must remain first-class.** Do NOT collapse
   them under a "Library" tab. The five states surface as the primary tabs / left-rail
   facets on Research Feed:
   - `inbox` — unprocessed (subtitle: "unprocessed")
   - `reading_list` — queued for later (subtitle: "queued")
   - `reading` — in progress (subtitle: "in progress")
   - `done` — archived (subtitle: "archived")
   - `trash`
2. **⌘K topbar is global.** Every authenticated page shows it. Tapping it opens a
   command palette (search papers, jump to project, run actions). Visible search box,
   not a hidden hotkey.
3. **No new lifecycle names.** Use the strings already in the DB.
4. **Preserve the gradient hero** — it's signature brand. Use it ONLY on the My Day
   intent card. Do not splatter it across other screens.

---

## 2 · Design tokens

Import `handoff/tokens.css` at the root. Map to Tailwind via `theme.extend.colors` if
you're on Tailwind; for now CSS variables are enough.

Key principles:
- **Two surfaces only.** `--jv-paper` (cream) for the canvas, `--jv-surface` (white)
  for cards / inner panels. Never invent a third gray.
- **Ink ladder:** `--jv-ink` → `--jv-ink-2` → `--jv-muted` → `--jv-faint`. Four levels,
  no more. UI text never goes below 13px.
- **Type roles:**
  - `--jv-serif` for: H1/H2, body copy inside *reading regions* (paper text, notes,
    learning-card content), italicised subtitles.
  - `--jv-sans` for: nav, buttons, table cells, dense UI.
  - `--jv-mono` for: numerics, scores, keyboard shortcuts, dates, § markers.

---

## 3 · Component inventory (build these first)

In rough dependency order. Each is small; specs are in section 5.

| Component | Role |
|---|---|
| `<Marker>` | The § ALL-CAPS small-tracked caption used everywhere |
| `<Pill>` | Status/tag chip; 6 tones (default/violet/rust/ok/warn/danger/ghost) |
| `<Btn>` | Button; kinds default/secondary/ghost/violet/danger; 3 sizes |
| `<ScoreChip>` | Mono number + 3-bar visualisation (violet/rust/ok) |
| `<Hair>` | Thin horizontal divider |
| `<BrandMark>` | Logo + wordmark + tagline "research log" |
| `<Topbar>` | Slim sticky topbar w/ breadcrumb + ⌘K search + right slot |
| `<FacetRail>` | Left rail of grouped, counted facets (used in Feed + Settings TOC) |
| `<WorkshopRail>` | Right-side tabbed panel (Notes / Chat / Cards / More) |
| `<KbdHint>` | Boxed keyboard shortcut chip |

Build them headless / props-driven. No screen-specific logic.

---

## 4 · Layout shells

Three reusable shells. Every screen is composed from one of these.

### 4a · `<EditorialShell>` (single column, narrow)
- Used by: My Day, Analytics, the *focused-section* parts of Settings, Admin.
- Max content width: 820px, centered.
- Padding: 36px top, 56px sides on desktop.
- Header pattern: `<Marker>` strap → `<h1 class="jv-h1">` → italic subtitle.

### 4b · `<ThreePaneShell>` (rail · content · rail)
- Used by: Research Feed, Paper Detail, Projects.
- Grid: `200px 1fr 320px` (configurable). Collapses to single column < 1100px.
- Left rail = facets / sections / chapter list.
- Center = primary content (scrollable independently).
- Right rail = preview / workshop (scrollable independently).

### 4c · `<FocusShell>` (single centered card)
- Used by: Learning (flashcard session), modals.
- Card on cream background, max-width 720px, centered vertically.

All shells get the `<Topbar>` at the top.

---

## 5 · Per-screen specifications

### 5.1 · Sidebar (global nav)

**Layout:** Always-on left rail, 256px wide. Three "verb" sections + Settings footer.

```
─────────────────────────────
[J] Jarvis
    research log
─────────────────────────────
⌕  Jump to…           ⌘K
─────────────────────────────
I   Today
    What needs your attention right now.
    · My Day
    · Pulse Deck            4
    · Inbox                12
─────────────────────────────
II  Read
    Your library, projects, the graph.
    · Research Feed
    · Projects              7
    · Knowledge Graph
    · Citations
─────────────────────────────
III Reflect
    What you learned and how you spent time.
    · Flashcards       due 8
    · Analytics
─────────────────────────────
[avatar] Ferhat    ⚙  🔔
```

**Rules:**
- Roman numerals in `--jv-mono` `--jv-faint`.
- Verb headings: serif, 22px, weight 500.
- Sub-blurb under each verb: italic serif 12px muted.
- Active item: white background, 2px inset violet left-shadow, weight 500.
- Counts: mono, right-aligned, faint when inactive, violet when row is active.

### 5.2 · Research Feed

**Shell:** `<ThreePaneShell>`.

**Left rail — `<FacetRail>`:**
- Group: Status (the 5 lifecycle states) — clicking changes which papers populate the center.
- Group: Star — `Starred 9`
- Group: Source — arXiv / Semantic Scholar / OpenAlex / PubMed (with counts)
- Group: Topic — derived from extraction tags

**Center:**
- Top: H1 = current lifecycle label + count + sort selector
- Row list (not cards). Each row:
  - `<ScoreChip>` on the left
  - Serif title (15px, weight 500) + meta line (authors · source · date) below
  - Star icon if starred, right-aligned
  - Whole row is hover-able; active row has 2px inset violet left-shadow + white bg
- Bottom-of-list infinite scroll.

**Right rail — preview pane:**
- "§ Preview · N of M" marker
- Serif title (19px)
- Meta line
- ScoreChip + tag pills
- "§ Brief" section with first 3 sentences of summary (serif body)
- CTA stack: `Open paper` (violet primary), Save / Skip secondary row.

**Removed surface tabs (Search, Ask):** these merge into ⌘K in the topbar.
**Pulse Deck:** a button at top of center pane labeled "Swipe mode" — toggles to a
single-card-at-a-time triage view. Not a route.

### 5.3 · Paper Detail

**Shell:** `<ThreePaneShell>`.

**Slim sticky topbar (replaces the current chunky title block):**
- Breadcrumb: Library / Inbox / Paper #N
- Lifecycle pill (violet "Reading" etc)
- Truncated title (single line, serif)
- `<ScoreChip>` · `Mark Done` · `Re-analyze` · star · trash

**Left rail — section nav:**
- "§ Sections": Brief · Key findings · Methodology · Limitations · Evidence · Cross-refs · Contradictions · Your notes · Chunks
- Anchor-link scroll; active section gets violet inset shadow
- Below: "§ Pipeline" with the analyze-paper checklist

**Center — single scrollable doc** (no tabs!):
- Width: 640px max, padding 32px 48px
- Marker strap → Serif title (32px) → italic author line → meta line
- Body sections each preceded by a § marker, serif 17.5px body
- Key findings: bordered-left blockquotes with page-anchor pills

**Right rail — `<WorkshopRail>` with tabs:**
- Notes (count) · Chat · Cards (count) · More
- Tab content scrolls independently
- "Add note" composer at top of Notes tab

### 5.4 · My Day

**Shell:** `<EditorialShell>`.

**Sections in order:**
1. Date strap: `§ Entry 247 · Tuesday` + ISO date in mono
2. H1 greeting: serif 44px ("Good morning, Ferhat.")
3. Italic subtitle: one sentence summarising state ("Four papers need a decision, the
   RGS chapter has been waiting two days, and your flashcard streak is on day 28.")
4. **Intent card** — the gradient hero block. Single CTA: "Resume · 47 min" with the
   committed-to focus carried over from yesterday. Secondary: "Change focus."
5. **Triage queue** — numbered list (01, 02, …) of papers needing a lifecycle
   decision, with row-level "Read now / Later / Skip" buttons.
6. **Tonight footer** — two-column: "Flashcards · N due" + "Tonight" (an open
   question / reading goal).

**Explicitly NOT on this page:**
- No metrics tile grid (those live in Analytics)
- No "this week" calendar
- No notifications feed

### 5.5 · Projects

**Shell:** `<ThreePaneShell>`.

**Concept:** A project is a **chapter**. Use roman numerals.

**Left rail — chapter list:**
- "§ Chapters · 7"
- Each row: roman numeral + serif name + status/papers/open-questions sub-line.

**Center — chapter body:**
- Marker strap: "§ Chapter I" + status pill
- H1 = chapter name (serif 38px)
- Italic subtitle = working title + due date
- "§ Open questions" — numbered Q1/Q2/Q3 list of research questions
- "§ Recent activity" — timeline (Added / Note / Done events)

**Right rail:**
- "§ Papers in chapter" — list with ScoreChip + lifecycle label
- "§ Flashcards from this chapter" — count + due + CTA

### 5.6 · Analytics

**Shell:** `<EditorialShell>`.

**Sections:**
1. Marker + H1 "Reflect" + italic subtitle ("What you learned, and how you spent
   your time, since [date].")
2. **Hero stat row** — 3 columns: Papers extracted / Focus hours / Cards reviewed.
   Each = marker label + serif 44px number + delta in mono.
3. **Reading cadence** — bar chart, last 14 days, today highlighted violet.
4. **What you read about** — horizontal bar list by topic, colored by topic family.

Not a dashboard. Reads like a one-page journal review. Date-range selector top-right.

### 5.7 · Learning (flashcards)

**Shell:** `<FocusShell>`.

**Topbar:** breadcrumb + "3 of 12" mono marker + "End session".

**Card:**
- White card, 720px wide, padding 40px 44px.
- Top: "§ Card 3 · [Deck name]" + last-seen pill.
- Question: serif 32px.
- Hair divider.
- Answer: violet § marker + serif 17px body, with attribution blockquote.

**Rating row (below card):**
- 4 cards: Again (red) / Hard (warn) / Good (violet) / Easy (ok).
- Each shows: label, keyboard shortcut chip (1/2/3/4), next-due estimate.

**Bottom hint:** "Press space to flip · 1-4 to rate · e to edit"

**Keyboard shortcuts (must implement):** Space = flip · 1/2/3/4 = rate · e = edit · u = undo.

### 5.8 · Settings

**Shell:** `<ThreePaneShell>` but with no right rail (use a 2-col 280px + 1fr).

**Left rail — editorial table of contents:**
Seven sections with roman numerals: Account · Sources · Models · Pipeline · Integrations · Appearance · Data.

Each section header is serif 17px with its roman numeral, followed by sub-items.
Active sub-item gets the violet inset shadow treatment.

**Right column — focused section body:**
- Marker + H1 + italic subtitle (same pattern as everywhere)
- Form fields with `<label>` (sans 13.5px weight 500) + italic hint (serif 12px muted) + input
- Save / Reset at the bottom-right, never sticky.

### 5.9 · Admin · Users

**Shell:** `<EditorialShell>` but wider (920px max).

- Marker + H1 + italic stats subtitle
- Search box + role pills
- Clean table: avatar + name (serif) / email (sans muted) / role pill / paper count
  (mono) / last active / overflow menu.
- No fancy stuff. Utilitarian.

---

## 6 · Onboarding affordances (build alongside, not after)

These are what convert "Bold + powerful" into "Bold + intuitive":

1. **First-run coachmark tour** on Feed and Paper. 4 steps each. Use `react-joyride`
   or roll-your-own with the existing Pill primitive. Store completion in
   `localStorage.jv_tour_v1`.
2. **⌘K command palette** — fuzzy search over: papers, projects, settings sections,
   actions ("Mark all read", "Sync arXiv now"). Use `cmdk` (kbar) or build with
   `Downshift`.
3. **Empty states teach.** First-run inbox shows a "How the pipeline works" card
   (Inbox → Reading list → Reading → Done) with an example paper. Don't show a void.
4. **Keyboard cheatsheet behind `?`** — modal overlay. Group by screen.
5. **Persistent breadcrumbs** on three-pane layouts.

---

## 7 · Migration order (suggested)

| Phase | Scope | Validates |
|---|---|---|
| 1 | Tokens + base primitives (Marker, Pill, Btn, ScoreChip, Hair) | Visual language is right before any screen lands |
| 2 | New Sidebar + Topbar with ⌘K stub | Global shell, every screen below sits inside this |
| 3 | Research Feed (3-pane, lifecycle tabs, preview rail) | Highest-traffic screen — proves the 3-pane pattern |
| 4 | Paper Detail (3-pane, single doc, workshop rail) | Reading workflow |
| 5 | My Day (editorial shell) | Most-visited entry point |
| 6 | Projects + Analytics | Editorial pattern at scale |
| 7 | Learning + Settings + Admin | Polish |
| 8 | Onboarding (coachmarks, ⌘K, empty states, cheatsheet) | Wider audience readiness |

Ship phase 1 + 2 to staging before touching screen migrations. Get a teammate to
click through and confirm the visual language reads correctly.

---

## 8 · Things you might be tempted to do — DON'T

- ❌ Add a "compact mode" toggle to undo the editorial type sizes. The serif H1s and
  larger spacing are the brand. If a screen feels too sparse, you have too few
  elements — don't shrink the type.
- ❌ Replace the cream `#faf8f3` with white globally. Cream is the canvas. White is
  only for cards/rails.
- ❌ Use emoji as iconography. Stick to the stroke-1.6 outline icon set.
- ❌ Add a gradient background to other CTAs to "match the hero." Hero gradient is
  reserved for the My Day intent card. Buttons use solid `--jv-ink` or `--jv-violet`.
- ❌ Surface metrics in the sidebar or My Day. Metrics live on Analytics only.
- ❌ Rename lifecycle states or introduce synonyms ("Queue", "Library", "Archive").
  The five backend names are also the UI names.

---

## 9 · What's in this hand-off package

```
handoff/
├── IMPLEMENTATION.md   ← this file
├── tokens.css          ← drop-in CSS variables + utility classes
└── screens/            ← reference screenshots (run screenshots/render.html if missing)
redesigns/
├── shared.jsx          ← React versions of the primitives, for reference
├── sidebars.jsx        ← Current / Safe / Bold sidebar variants
├── feeds.jsx           ← Current / Safe / Bold Research Feed variants
├── papers.jsx          ← Current / Safe / Bold Paper Detail variants
├── screens-bold-1.jsx  ← My Day / Projects / Analytics (Bold)
└── screens-bold-2.jsx  ← Learning / Settings / Admin (Bold)
Redesigns.html          ← interactive canvas to study all variants side-by-side
screenshots/render.html ← Single-screen renderer (use ?#feed-bold etc)
```

To regenerate screenshots: open `screenshots/render.html`, set the URL hash to a
screen name (e.g. `#feed-bold`), and screenshot the page.

---

## 10 · Open questions for the user before you start

1. **Stack:** Tailwind or vanilla CSS-modules? Tokens are framework-agnostic but
   utility-class mappings differ.
2. **Routing:** Are the verb-grouped routes (`/today`, `/read`, `/reflect`) acceptable,
   or must existing URLs (`/feed`, `/projects`) stay stable for shared links?
3. **⌘K backend:** Is there a search endpoint that handles "papers + projects + actions"
   in one call, or do we need to fan out client-side?
4. **Offline mode for Paper Detail:** in scope for v1 or follow-up?
5. **Annotation:** PDF.js + highlight persistence — v1 or follow-up?
