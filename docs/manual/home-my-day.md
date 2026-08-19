<!-- verified-against-UI: 2026-08-19 | routes: /, /my-day -->

# My Day & Home

Two daily surfaces with different jobs. **My Day** is where you work: one thing to do now, and everything else folded away until you ask for it. **Home** is the instance dashboard — counts, setup progress, and the batch operations that prepare papers you already have.

---

## My Day — `/my-day`

<!-- screenshot: /my-day — DateMasthead at top, HeroNow section with HeroThread visible, and ProjectsSection below -->

The page opens with only the daily loop visible.

### The date

Today's date and weekday sit at the top, so a page left open overnight cannot quietly pretend it is still yesterday.

### Yesterday

A short account of what the previous session produced. It hides itself entirely when there was no recorded activity, rather than showing a row of zeros.

### Now

The one thing to work on. **Now** offers up to four choices as small tabs, and only the ones that apply to you appear:

| Tab | Appears when |
|-----|--------------|
| **Pulse #1** | Always — the top card of today's [Pulse](pulse.md) deck |
| **Resume thread** | You have an open reading thread |
| **Continue task**, or **On break** | A focus interval is running or paused |
| **Resume reading** | A paper is marked Reading |

JARVIS picks a sensible starting tab, but an explicit choice wins and is remembered. If no Pulse deck exists yet, the Pulse tab offers to generate one instead of reporting an error.

### Intent

A one-line statement of what you want to get done today. It is saved and shown back to you in later sessions until you change it.

### Today's Pulse

A preview of the top cards from today's deck, linking through to `/pulse` for the full deck and rating.

### More

Everything episodic sits behind a row of **More** chips at the foot of the page. Each chip expands the real section in place — nothing here is a summary of something else:

| Chip | What opens |
|------|-----------|
| **Projects** | Your active projects and their recent activity, each linking to [Projects](projects.md) |
| **Open threads** | Papers you started and have not finished, each with a Resume button |
| **Triage** | Papers that arrived in your Inbox today: save, discard, or open |
| **Learning & focus** | Cards due, your streak, and an estimated time for today's review, linking to [Learning Cards](learning-cards.md) |
| **Weekly digest** | Papers ingested, cards reviewed, and topics covered over the past seven days |
| **End of day** | A reflection prompt you can save as a note |

Chips are independent: open as many as you want, and they stay open until you close them or leave the page.

---

## Home — `/`

The Home page, titled **Dashboard**, is the instance overview.

<!-- screenshot: / — Home page showing MetricTileGrid, SetupBanner, and onboarding checklist -->

### Setup banner and first steps

If installation setup is not finished, a banner appears at the top; see [Getting Started](getting-started.md). Alongside it, a **Welcome to JARVIS Research Assistant** card tracks three first-use steps — **Add a research topic**, **Fetch your first papers**, and **Analyze a paper** — each with a shortcut button (Go to Settings, Open Papers). Steps check themselves off as you complete them, the card disappears once all three are done, and you can dismiss it early with the × button. Completing all three shows a one-time "All set! Happy researching." note.

### Metric tiles

Five counts, each a link: **Papers** (`/feed`), **Due Cards** (`/cards`), **Active Projects** (`/projects`), **Topics** (`/settings`), and **Scheduled Jobs** (`/settings`). They are read-only.

### Prepare your papers

Queue work for papers already in your library. Each button confirms first, then reports what it queued.

| Button | What it does |
|--------|-------------|
| **Process all papers** | Queues download, PDF processing, and summaries for every saved paper that still needs them |
| **Process PDFs** | Runs text extraction and chunking for papers that have a local PDF but have not been processed |

Behind an **Advanced** disclosure:

| Button | What it does |
|--------|-------------|
| **Summarize** | Generates summaries for processed papers that do not have one yet |
| **Extract Entities** | Runs entity extraction for the Knowledge Graph. Administrators only; other accounts see a line explaining that in its place |

Queued work appears in the jobs panel with a progress bar and, when it finishes only partly, a line reading how many items failed, were skipped, and were not processed out of the total. You can cancel a running job — it stops after the step in flight. Finished work stays finished, and re-running an operation for the papers that were skipped is safe. When there is nothing to queue, the button says so instead of creating an empty job.

---

## Related pages

- [Getting Started](getting-started.md) — the setup banner and the first-run wizard.
- [Pulse](pulse.md) — the full deck previewed on My Day.
- [Papers & Discover](research-feed.md) — where triage and Papers live.
- [Learning Cards](learning-cards.md) — the review session linked from Learning & focus.
