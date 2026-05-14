# v5 Design Spec — JARVIS My Day (Calm Ritual v2)

This is the complete design spec for `/my-day`. Sections are ordered top-to-bottom. Every measurement and color is load-bearing — the system depends on consistent vertical rhythm.

---

## 0. Design system (global)

### Type stack
| Role | Family | Notes |
|---|---|---|
| Body / UI | **Inter** | 400, 500, 600, 700 |
| Paper titles, slogans, large headers | **Source Serif 4** | 300–700 (variable opsz 8–60); use for headlines and serifed quotes only |
| Numerics, metadata, kbd hints, § markers | **JetBrains Mono** | 400, 500, 600 |

### Color tokens
| Token | Light | Dark | Use |
|---|---|---|---|
| `--surface-paper` | `#fbfaf7` | `#0e0e10` | Page background |
| `--surface-card` | `#ffffff` | `#161618` | Cards, popovers |
| `--surface-cream` | `#fdf9f0` | `#1a1814` | Hero card warm side |
| `--surface-cool` | `#f5f8fe` | `#13161e` | Hero card cool side |
| `--ink-blue` | `#0b3a8a` | `#7ba2f0` | Primary accent (links, active nav, CTAs) |
| `--text-strong` | `zinc-900` | `zinc-100` | Headings, body |
| `--text-soft` | `zinc-700` | `zinc-300` | Secondary body |
| `--text-meta` | `zinc-500` | `zinc-400` | Metadata, timestamps |
| `--text-faint` | `zinc-400` | `zinc-500` | Tertiary metadata |
| `--border-hair` | `zinc-200` | `zinc-800` | Hairline borders |

### Layout
- Page max-width: **860px**, centered, `px-10 py-10`
- Vertical section rhythm: **48px** (`space-y-12`) between top-level `<section>` blocks
- Section header rhythm: **12px** (`mb-3`) between `§ marker` and section content

### § Section header pattern
```tsx
<div className="flex items-baseline gap-3 mb-3">
  <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">§ Section name</span>
  <span className="font-mono text-[10px] text-zinc-400">contextual count or note</span>
</div>
```

### Section IDs (for anchor jumps via `j`/`k` keys)
`#now`, `#intent`, `#projects`, `#threads`, `#pulse`, `#triage`, `#cards`, `#eod`

---

## 1. Topbar (shell change)

Existing topbar gains an active-task Pomodoro chip:

```tsx
<button className="h-7 inline-flex items-center gap-1.5 px-2 rounded-md border border-zinc-200 bg-zinc-50 hover:bg-white">
  <Clock className="h-3 w-3 text-[#0b3a8a]"/>
  <span className="font-mono tabular-nums text-[11px] font-medium">23:48</span>
  <span className="text-[10px] text-zinc-500 max-w-[120px] truncate">{activeTask.title}</span>
</button>
```

Order from left: search · jobs indicator · **Pomodoro chip** · keyboard-shortcuts button · avatar.

---

## 2. Date masthead (top of page)

```
RESEARCH LOG · ENTRY 247 · 09:14
Tuesday, March 17.
"What we are looking for is what is looking." — St. Francis
```
- "RESEARCH LOG..." line: `font-mono text-[10px] uppercase tracking-[0.22em] text-zinc-400`
- Date: `font-serif text-[36px] leading-[1.1] tracking-tight text-zinc-900`
- Quote: `font-serif italic text-zinc-500 text-[15px]`. Quote source = TBD; pull from a small rotating set keyed off date.

**Right side**: 4 mini-stats (`pulse / due / tasks / new`), each clickable, links to its anchor. Numbers in `text-[14px] font-semibold text-zinc-900`, labels `text-[9px] uppercase tracking-wider`.

---

## 3. § Yesterday

A single block, no card chrome. List of 2–4 items with check / chevron icon prefixes.

- Completed items: `<Check>` icon green, body text `text-zinc-700 text-[13.5px]`
- Deferred items: `<ChevronRight>` icon zinc-400, body strikethrough zinc-500, with inline `carry over →` link in `--ink-blue`

Header note: `{focused}h focused · {cards_reviewed} cards · {tasks_done} tasks done`

---

## 4. § Now (HERO)

The signature element. Single rounded card, gradient background, mode picker top-right.

### Card chrome
```tsx
<div className="rounded-xl border border-[#0b3a8a]/15 bg-gradient-to-br from-[#fdf9f0] via-white to-[#f5f8fe] p-7 relative overflow-hidden shadow-[0_1px_0_rgba(0,0,0,0.02)]">
  {/* Two soft blob backgrounds: top-right [#0b3a8a]/5, bottom-left amber-100/40, both blur-3xl */}
</div>
```

### Mode picker (top-right of section header)
3 segmented buttons: **Pulse #1 · Resume thread · Continue task**. 
Active state: `bg-white text-zinc-900 shadow-sm`. Inactive: `text-zinc-500 hover:text-zinc-800`.
Container: `bg-zinc-100/80 rounded-md p-0.5 flex gap-0.5`. Buttons are `h-6 px-2.5 rounded text-[10.5px] font-mono`.

### Mode 1: Pulse (default)
- Pill `Next` (ink-blue solid) + meta line "Triage today's pulse · ~6 min · #1 of {count}"
- **Title**: paper title, `font-serif text-[26px] leading-[1.18] tracking-tight max-w-[24ch]`, hovers to ink-blue
- Authors line in mono
- TL;DR paragraph, `text-[14px] leading-relaxed max-w-[64ch]`
- **Why** chips: small `--ink-blue` soft pills
- **Score row**: 4-stop stacked bar (emb · llm · rec · graph), with score number `0.94` left of bar
- **Buttons** (in order): `Open & start focus` (primary ink-blue) · `Accept` (outline) · `Skip` (ghost) · `Save for later` (ghost)
- Right-aligned kbd hint: `⏎ open · ⌥+a accept`

### Mode 2: Thread
- Pill `Resume` + meta "closest to done · 85% · last touched 09:02"
- Thread title (`font-serif text-[24px]`), section anchor in italic serif
- Body: "You were 85% through… Estimated 18 min to close out…"
- Progress bar
- Buttons: `Resume thread` (primary) · `Start 25-min focus` (outline) · `View all threads` (ghost)

### Mode 3: Task
- Pill `Continue` + meta "interrupted yesterday · 23:48 in last session"
- Task title (`font-serif text-[24px]`)
- Project badge (color-coded outline) + priority + estimated remaining
- Body: "Pomodoro session paused at 23:48… Bookmarked at p. 12 (§4.1)"
- Buttons: `Resume Pomodoro (1:12)` (primary) · `Open paper` (outline) · `Mark done` (ghost)

### Smart default algorithm
On first visit each day:
1. If a Pomodoro session was paused yesterday with <30 min remaining → **Task mode**
2. Else if a thread is at >70% progress and last_touched_at < 24h → **Thread mode**
3. Else → **Pulse mode**

Cache the user's manual choice in `localStorage('myday.heroMode')` and prefer it for the rest of the session.

---

## 5. § Today's intent

The block that turns "today" into a sentence.

- **Intent line**: a single sentence, `font-serif text-[19px] leading-snug tracking-tight max-w-[58ch]`, with a 2px `--ink-blue` left border (`border-l-2 pl-5`)
- Below intent: project pill + small "▶ start a 25-min block" link
- "edit" link top-right (small mono)
- **Tasks ladder** (below the intent block, indented `pl-5`):

### Task row
```
01  ●  Reread §4 of Kidger 2022 on adjoint methods       [thesis-ch3]   ▶ focus   ✕
```
- **Number**: `font-mono text-[10px] text-zinc-400 tabular-nums w-5`
- **Complete circle**: `h-3.5 w-3.5 rounded-full border-[1.5px]`. Top task gets `border-[#0b3a8a]`; others `border-zinc-300`. Hover: `bg-[#0b3a8a]/10`.
- **Title**: `text-[13.5px] text-zinc-700 group-hover:text-zinc-900 truncate flex-1`
- **Project badge**: `font-mono text-[10px] px-1.5 py-0.5 rounded border` with the project's color as both `borderColor` and `color`. Click → `/projects?projectId=X`.
- **▶ focus button**: `opacity-0 group-hover:opacity-100 h-6 px-2 text-[10px] font-mono rounded text-[#0b3a8a] hover:bg-[#0b3a8a]/5`. Click → `pomodoroStartWork({task})` (this binds the topbar timer to the task).
- **✕ delete**: same hover pattern, hover red.
- Row hover: `-mx-2 px-2 rounded-md hover:bg-white/60`.

### Quick-add row
```tsx
<button className="flex items-center gap-2 text-[12px] font-mono text-zinc-400 hover:text-zinc-900 ml-8 mt-1">
  <Plus/> add task <Kbd>⌘</Kbd><Kbd>+</Kbd>
</button>
```

### Completed-today expandable footer
Toggle button: `▸ {n} done today` / `▾ {n} done today`. When expanded, list completed tasks in zinc-400 with strikethrough and right-aligned timestamp.

---

## 6. § Projects

3 active projects, no card chrome — each row is a link.

```
[●  green dot] Thesis Ch.3 — Stiff Neural ODEs                                64%
                ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  First full draft · Mar 28
```

- Status dot: `h-1.5 w-1.5 rounded-full`, `bg-emerald-500` if on-track, `bg-amber-500` if at-risk
- Name: `text-[13.5px] font-medium`, hover ink-blue
- Progress %: `font-mono text-[10.5px] tabular-nums text-zinc-400`
- Bar: `h-1 rounded-full bg-zinc-100` overflow-hidden, fill in project color
- Right meta: milestone · due (`font-mono text-[10.5px] text-zinc-500 tabular-nums`)
- 3-color rotation: `#2563eb`, `#16a34a`, `#9333ea`. (Or pull from project `color` field.)

---

## 7. § Open threads

3 thread rows, each a left-bordered block (`border-l border-zinc-200 pl-5 py-1`).

- Title: `text-[13.5px] text-zinc-900 leading-snug`
- Anchor: `text-[12px] text-zinc-500 italic font-serif` prefixed with `↳`
- Bottom row: progress bar (max-w 140px) · % + last_at · `resume →` (right-aligned, opacity-60 → 100 on hover, ink-blue)
- On hover: left-border becomes `--ink-blue`

---

## 8. § Today's pulse

Top 5 papers as inline list (rank 2–5 shown by default; rank 1 is in the hero). Below: "show {n} more ▾" link.

### Pulse row layout
3-column grid: `[28px _ 1fr _ auto]` gap-4.

- **Column 1**: rank `#2`, mono zinc-400
- **Column 2** (the body):
  - Title: `font-serif text-[16.5px] leading-snug tracking-tight`, hover ink-blue
  - Authors line in mono
  - TL;DR (`text-[13px] leading-relaxed max-w-[60ch]`)
  - Score row: `0.81` (mono w-9) · 4-stop stacked bar (w-32) · 3 mono tags `#cde #signatures #time-series`
- **Column 3**: 3 icon buttons (ThumbsUp / ThumbsDown / Bookmark), `opacity-50 group-hover:opacity-100`

Header right side: `archive →` link + `regenerate` button (ghost, with sparkles icon).

---

## 9. § Triage

Combines `action_items` and `missing_foundational` into one rounded-lg card with hairline divides between rows.

### Header right
`Process all (N)` button (soft tone) — calls a batch ingest endpoint when N papers have `state=pdf-ready`.

### Row layout (grid: `[110px _ 1fr _ auto]`)
- **Column 1** (110px): a Pill — "Foundational" (warn), "Needs index" (neutral), or "No PDF" (warn)
- **Column 2**: 
  - Paper title, `text-[13px] font-medium leading-snug truncate`, hover ink-blue
  - Meta (citation count + reason for foundational; source for action items): `text-[11px] text-zinc-500 mt-0.5 font-mono`
- **Column 3**: action button(s) — `Add & process` for foundational, `Process` for pdf-ready, `✕` for dismiss

---

## 10. § Learning & focus

2-column grid (`grid-cols-2 gap-4`), both cards `rounded-lg border border-zinc-200 bg-white p-4`.

### Learning cards card

- Header: `Learning cards` icon + uppercase mono label
- **CTA condition**: if `cards.due > 0`, show on the right an **orange button** `Review now →` (`bg-orange-500 hover:bg-orange-600 text-white`)
- Body: orange-tinted alert (`bg-orange-50 border border-orange-100`) with the count in `text-[24px] font-bold tabular-nums text-orange-800`
- If `cards.due === 0`: body is "No reviews pending. ✓" in muted text
- Footer stats: streak (with flame icon) · cards reviewed today · 30d retention %

### Focus today card

- Header: `Focus today` clock icon + `Start 25:00` button (primary ink-blue)
- Body: `1.4h / 4h target` with progress bar
- Footer: streak + small text "last: 23:48 on '{active task title}…'"

This card is a **summary** — the active timer lives in the topbar.

---

## 11. § End of day

3 reflection prompts as dashed-underline inputs. The whole block submits to `POST /journal/today` (single document keyed by date).

- Each prompt has a mono uppercase label and a serif italic placeholder
- Input style: `bg-transparent border-0 border-b border-dashed border-zinc-300 px-0 py-1.5 font-serif italic text-[14.5px]`
- Focus state: solid ink-blue underline

Prompts (frozen v1 set):
1. "One thing that worked"
2. "What's still blocking me"
3. "First move tomorrow"

---

## 12. Footer

`pt-6 pb-2 border-t border-dashed border-zinc-200`, two-column.

- Left: `end of entry {n}` (mono, zinc-400)
- Right: 3 keyboard hints — `j k jump section` · `⌘. command mode` · `⇧↩ seal day`

---

## States & edge cases

| Condition | Behavior |
|---|---|
| No tasks today | Tasks ladder shows only the quick-add row + a `font-serif italic text-zinc-400` "Set today's intent first." line above it |
| No threads | Hide § Open threads entirely; remove from anchor jump |
| Pulse not yet generated | Hero shows "Pulse runs at 06:00 — generating now…" with a spinner; mode picker disabled |
| 0 cards due | Learning Cards card shows "No reviews pending ✓"; no orange CTA |
| Triage empty | Hide § Triage entirely |
| EOD already submitted today | Show prompts as filled, italic, with a small "edit" affordance and `sealed at 22:14` mono timestamp |

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `j` / `k` | Jump to next / previous section anchor |
| `?` | Open shortcut dialog |
| `⏎` (when hero focused) | Open hero target |
| `⌥+a` (in hero pulse mode) | Accept current pulse pick |
| `⌘+.` | Toggle Command-mode (out of scope for v5; v3 will live on this key) |
| `⇧+↩` | Submit EOD reflection |
| `⌘+K` | Search (existing) |
