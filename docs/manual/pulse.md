<!-- verified-against-UI: 2026-07-25 | routes: /pulse -->

# Pulse

The **Pulse** page at `/pulse` shows your daily recommendation deck — a curated set of papers selected by the Pulse engine based on your configured research topics, reading history, and ratings.

<!-- screenshot: /pulse — PulseDeck showing three PulseCards with WhyPopover open on one card, and a StaleBadge in the deck header -->

---

## The Pulse deck

### PulseDeck

The deck is a collection of **PulseCards**, each representing a recommended paper. Cards are generated in the background by a scheduled job (configured in Settings → Automation or Settings → Pulse) and are available when you open the page. If no deck has been generated yet, a prompt invites you to generate one manually.

The optional Telegram delivery reads this same ranked deck and shows its first
five cards for a shorter mobile presentation. It does not run a second ranking
policy. A paper can appear again on another day when relevance, source results,
and your feedback are unchanged; Pulse does not promise daily novelty.

### PulseCard

Each card in the deck shows:

- Paper title and authors
- A short relevance excerpt
- Action buttons for rating and opening the paper

When JARVIS has not yet learned from enough of your feedback, the deck labels its ordering as **Basic ranking (learning from your feedback)**. Once a learned model is available, that caption is no longer shown; the rest of the deck remains usable in either state.

### WhyPopover

Clicking the **Why?** indicator on a card opens the **WhyPopover**, which explains the relevance score for that card — which of your research topics matched, what signals drove the recommendation (e.g. topic keyword overlap, citation proximity, past ratings), and the numerical relevance score.

### StaleBadge

If today's deck has not been regenerated and the one shown is from an earlier day, a **StaleBadge** appears in the deck header (not on individual cards) to signal that the recommendations may not reflect your most recent reading activity or topic configuration. Clicking it opens a sheet with per-source diagnostics and a **Generate now** button to request a fresh deck.

Telegram carries the same distinction in plain text: it identifies a current or
earlier deck and its age, states when ranking used reduced signals, and labels
each card's evidence check as verified, a reported confidence level,
unverified, or not reported. These labels describe available evidence; they do
not independently establish that a paper's claims are correct.

---

## Generating a new deck

Click the **Generate new deck** button to request a fresh Pulse deck. Generation runs as a **background job**; the page shows a progress indicator while the job is running. Depending on the number of papers in your library and the complexity of your topics, generation may take a few seconds to a minute.

Once complete, the new deck replaces the previous one.

Generation can also happen automatically on a schedule. See [Settings](settings.md) (§IV System → Automation and Pulse sections) to configure the schedule.

---

## Rating cards

Each card has **thumbs-up** and **thumbs-down** rating buttons:

- **Thumbs up** — marks the paper as relevant. The rating feeds back into the Pulse recommendation model to surface similar papers in future decks; it does not add the paper to your Library.
- **Thumbs down** — marks the paper as not relevant. It is hidden from future decks for 60 days. A topic that collects repeated thumbs-down (5 or more within 90 days) is dampened: its positive similarity signal is halved — never boosted — when scoring future candidates, so a topic you keep rejecting quietly stops dominating your deck.
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
