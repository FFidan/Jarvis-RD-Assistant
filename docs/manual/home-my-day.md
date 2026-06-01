<!-- verified-against-UI: 2026-05-18 | routes: /, /my-day -->

# My Day & Home

---

## Home — `/`

The Home page is the first screen you see after signing in. It provides a quick-start dashboard for your research session and a system health overview.

<!-- screenshot: / — Home page showing MetricTileGrid, SetupBanner, and onboarding checklist -->

### MetricTileGrid

A row of **KPI tiles** summarising the current state of your library: total papers, papers due for review, unread items in the Inbox, and active projects. The tiles are read-only; click any tile to navigate to the corresponding surface.

### SetupBanner

If your account's setup wizard has not been completed, a **SetupBanner** appears at the top of the Home page. It links back to `/setup` so you can finish the remaining wizard steps. Once setup is complete, or once you dismiss the banner, it no longer appears. See [Getting Started](getting-started.md) for the full wizard flow.

### Onboarding checklist

Alongside the SetupBanner, an **onboarding checklist** tracks first-use milestones: adding a research topic, saving your first paper, running your first Pulse deck, and reviewing your first card. Items are checked off automatically as you complete them.

### Batch Operations

A section of the Home page provides **Batch Operations** that act across your entire library:

| Operation | What it does |
|-----------|-------------|
| Process PDFs | Run text extraction and chunking for all papers that have a downloaded PDF but have not yet been processed |
| Summarise | Generate summaries for all processed papers that have not yet been summarised |
| Extract Entities | Run entity extraction for all papers to populate the Knowledge Graph |

Each batch operation launches a background job. Progress is visible in the Jobs panel (accessible from the TopBar).

---

## My Day — `/my-day`

The **My Day** page gives you a focused daily research workspace. It is structured as a vertical sequence of sections, described below in order.

<!-- screenshot: /my-day — DateMasthead at top, HeroNow section with HeroThread visible, and ProjectsSection below -->

### DateMasthead

The date and day of the week are shown at the top of the page, grounding the session in the current day.

### YesterdaySection

A brief summary of what you accomplished in the previous session: papers saved, cards reviewed, tasks completed.

### HeroNow

The primary focus section for the current moment. Shows one of four **Hero** components depending on your current activity:

| Component | Shown when |
|-----------|-----------|
| **HeroThread** | You have an active reading thread (a paper you were reading and left mid-way) |
| **HeroTask** | You have an overdue or high-priority task in an active project |
| **HeroPulse** | A new Pulse deck is available and you have not rated any cards today |
| **HeroResumeReading** | A paper is marked as **Reading** and was last opened more than one day ago |

Only the highest-priority hero is shown at a time.

### IntentSection

An input where you can set your **research intent** for today — a short statement of what you want to accomplish. Your intent is saved and displayed in subsequent sessions until you update it.

### ProjectsSection

A compact list of your active projects with recent activity. Each project links directly to its detail view on the [Projects](projects.md) page.

### ThreadsSection

A list of reading threads — papers you started reading but have not finished or marked as Done. Each thread shows the paper title, how long ago you last opened it, and a **Resume** button.

### TodaysPulseSection

A preview of today's [Pulse](pulse.md) deck showing the top recommended cards. Click through to `/pulse` to see the full deck and rate cards.

### TriageSection

Papers that have arrived in your Inbox today, presented for quick triage: save to library, discard, or open for detail.

### LearningFocusSection

A summary of your spaced-repetition study for today: cards due, your current streak, and an estimated time to complete today's review. A **Review now** button links to `/cards`.

### WeeklyDigestSection

A compact weekly summary: papers ingested, cards reviewed, and topics covered over the past seven days.

### EndOfDaySection

A reflection prompt that appears in the afternoon or evening. It invites you to note what you learned or questions that came up, which can be saved as notes or project entries.

### Footer

Standard site footer with version information.

---

## Related pages

- [Getting Started](getting-started.md) — SetupBanner and onboarding checklist context.
- [Pulse](pulse.md) — full Pulse deck linked from TodaysPulseSection.
- [Projects](projects.md) — active projects linked from ProjectsSection.
- [Learning Cards](learning-cards.md) — review session linked from LearningFocusSection.
- [Research Feed & Library](research-feed.md) — Inbox triage linked from TriageSection.
