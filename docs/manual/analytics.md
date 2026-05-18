<!-- verified-against-UI: 2026-05-18 | routes: /analytics -->

# Analytics

The **Analytics** page at `/analytics` provides charts and statistics about your reading activity, library growth, and system usage over time.

<!-- screenshot: /analytics — DateRangeFilter at top, KpiBand below it, and two Recharts charts visible -->

---

## Date range filter

A **DateRangeFilter** at the top of the page lets you choose the time window for all charts. Available presets:

| Preset | Window |
|--------|--------|
| 7d | Last 7 days |
| 14d | Last 14 days |
| 30d | Last 30 days |
| 90d | Last 90 days |

Changing the date range updates all charts and the KPI band simultaneously.

---

## KPI band

The **KpiBand** shows a row of key-performance-indicator tiles summarising the selected period at a glance. Typical tiles include papers added, cards reviewed, and Pulse decks generated.

---

## Charts

All charts use **Recharts**. Each chart is scoped to the selected date range.

### ActivityChart

A time-series chart showing your daily activity — papers saved, notes added, reviews completed — over the selected window. Use this to spot gaps in your research cadence or identify productive periods.

### RetentionChart (FSRS)

A chart showing your spaced-repetition retention rate over time, derived from FSRS review ratings. A higher retention rate indicates that the review schedule is keeping facts accessible.

### PapersBySourceChart

A breakdown of how many papers were ingested from each source (arXiv, Semantic Scholar, OpenAlex, PubMed, PDF upload, Zotero) over the selected period.

### PapersByStatusChart

A breakdown of your library by reading state: Inbox, Reading, To Read, Done, Archived. Use this to gauge how much of your library is actively engaged versus waiting in queue.

### ReviewsByRatingChart

A bar chart of spaced-repetition review outcomes over the selected period, broken down by rating: Again, Hard, Good, Easy. A high proportion of **Again** ratings suggests a deck is too difficult or cards are too infrequent.

### LlmCostChart

A chart showing estimated LLM token usage and cost over the selected period, broken down by operation type (summarisation, extraction, card generation, RAG). Useful for monitoring resource consumption on instances that use paid cloud LLM providers.

---

## Related pages

- [Learning Cards](learning-cards.md) — the FSRS review system whose ratings feed the RetentionChart and ReviewsByRatingChart.
- [Research Feed & Library](research-feed.md) — the library whose growth is reflected in the PapersBySourceChart and PapersByStatusChart.
- [Pulse](pulse.md) — Pulse decks generated per period appear in the KPI band.
- [Settings](settings.md) — configure LLM providers whose cost is tracked in LlmCostChart.
