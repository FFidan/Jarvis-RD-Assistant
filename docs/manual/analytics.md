<!-- verified-against-UI: 2026-07-25 | routes: /analytics -->

# Analytics

The **Analytics** page at `/analytics` provides charts and statistics about your reading activity, library growth, and system usage over time.

<!-- screenshot: /analytics — DateRangeFilter at top, KpiBand below it, and two Recharts charts visible -->

---

## Date range filter

A **DateRangeFilter** at the top of the page lets you choose the time window for all charts. Available presets:

| Preset | Window |
|--------|--------|
| 7d | Last 7 days |
| 30d | Last 30 days |
| 90d | Last 90 days |

A **custom** field next to the presets accepts any whole number of days from 1 to 365.

Changing the date range updates all charts and the KPI band simultaneously.

---

## KPI band

The **KpiBand** shows three key-performance-indicator tiles summarising the selected period at a glance: **Papers Read**, **Focus Hours**, and **Cards Reviewed**. Papers Read and Focus Hours each carry a trend indicator comparing the tile to the previous period. Cards Reviewed shows your current review streak while one is running, and falls back to the same period-on-period trend when it is not.

---

## Charts

All charts use **Recharts**. Each chart is scoped to the selected date range.

### ActivityChart

A time-series chart showing your daily activity across three series — **Papers Read**, **Cards Reviewed**, and **Tasks Completed** — over the selected window. Use this to spot gaps in your research cadence or identify productive periods.

### RetentionChart (FSRS)

A chart showing your spaced-repetition retention rate over time, derived from FSRS review ratings. A higher retention rate indicates that the review schedule is keeping facts accessible.

### PapersBySourceChart

A breakdown of how many papers were ingested from each source (arXiv, Semantic Scholar, OpenAlex, PubMed, PDF upload, Zotero) over the selected period.

### PapersByStatusChart

A breakdown of your library by reading state: Inbox, To Read, Reading, Done, Trash. Use this to gauge how much of your library is actively engaged versus waiting in queue.

### ReviewsByRatingChart

A bar chart of spaced-repetition review outcomes over the selected period, broken down by rating: Again, Hard, Good, Easy. A high proportion of **Again** ratings suggests a deck is too difficult or cards are too infrequent.

### LlmCostChart

A chart showing estimated LLM token usage and cost over the selected period, broken down by operation type (summarisation, extraction, card generation, RAG). Useful for monitoring resource consumption on instances that use paid cloud LLM providers.

---

## Related pages

- [Learning Cards](learning-cards.md) — the FSRS review system whose ratings feed the RetentionChart and ReviewsByRatingChart.
- [Papers & Discover](research-feed.md) — the library whose growth is reflected in the PapersBySourceChart and PapersByStatusChart.
- [Settings](settings.md) — configure LLM providers whose cost is tracked in LlmCostChart.
