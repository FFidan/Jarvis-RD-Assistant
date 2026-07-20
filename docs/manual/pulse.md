<!-- verified-against-UI: 2026-07-20 | routes: /pulse -->

# Pulse

The **Pulse** page at `/pulse` shows your daily recommendation deck — a curated set of papers selected by the Pulse engine based on your configured research topics, reading history, and ratings.

<!-- screenshot: /pulse — PulseDeck showing three PulseCards with WhyPopover open on one card and a StaleBadge on another -->

---

## The Pulse deck

### PulseDeck

The deck is a collection of **PulseCards**, each representing a recommended paper. Cards are generated in the background by a scheduled job (configured in Settings → Automation or Settings → Pulse) and are available when you open the page. If no deck has been generated yet, a prompt invites you to generate one manually.

### PulseCard

Each card in the deck shows:

- Paper title and authors
- A short relevance excerpt
- The **StaleBadge** (see below)
- Action buttons for rating and opening the paper

When JARVIS has not yet learned from enough of your feedback, the deck labels its ordering as **Basic ranking (learning from your feedback)**. Once a learned model is available, that caption is no longer shown; the rest of the deck remains usable in either state.

### WhyPopover

Clicking the **Why?** indicator on a card opens the **WhyPopover**, which explains the relevance score for that card — which of your research topics matched, what signals drove the recommendation (e.g. topic keyword overlap, citation proximity, past ratings), and the numerical relevance score.

### StaleBadge

If a card was generated more than a configurable number of days ago, a **StaleBadge** appears on it to signal that the recommendation may not reflect your most recent reading activity or topic configuration.

### SourceTimeline

Below or alongside the deck, the **SourceTimeline** shows a chronological breakdown of when the papers in the current deck were published, giving you a sense of the recency distribution of your recommendations.

---

## Generating a new deck

Click the **Generate new deck** button to request a fresh Pulse deck. Generation runs as a **background job**; the page shows a progress indicator while the job is running. Depending on the number of papers in your library and the complexity of your topics, generation may take a few seconds to a minute.

Once complete, the new deck replaces the previous one.

Generation can also happen automatically on a schedule. See [Settings](settings.md) (§IV System → Automation and Pulse sections) to configure the schedule.

---

## Rating cards

Each card has **thumbs-up** and **thumbs-down** rating buttons:

- **Thumbs up** — marks the paper as relevant. It is added to your Library and the rating feeds back into the Pulse recommendation model to surface similar papers in future decks.
- **Thumbs down** — marks the paper as not relevant. It is hidden from future decks. A topic that collects repeated thumbs-down (5 or more within 90 days) is dampened: its positive similarity signal is halved — never boosted — when scoring future candidates, so a topic you keep rejecting quietly stops dominating your deck.
- **Save** — saves the paper to your Library without a quality rating.

---

## Opening a paper

Clicking the paper title or a dedicated **Open** button on a card navigates to the [Paper Detail](paper-detail.md) page for that paper. If the paper has not yet been fully processed (downloaded, chunked, summarised), you can trigger those steps from the Paper Detail actions rail.

---

## My Day preview

A condensed preview of today's Pulse deck also appears on the [My Day](home-my-day.md) page (TodaysPulseSection). The preview shows the top-rated or most relevant cards; click through to `/pulse` for the full deck.

---

## Related pages

- [My Day & Home](home-my-day.md) — today's Pulse preview and research focus.
- [Settings](settings.md) — configure the Pulse schedule and automation (§IV System).
- [Research Feed & Library](research-feed.md) — papers saved via Pulse appear in your Library.
