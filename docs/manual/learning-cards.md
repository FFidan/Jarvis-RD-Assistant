<!-- verified-against-UI: 2026-08-19 | routes: /cards, /cards?mode=review, /cards?mode=library -->

# Learning Cards

The **Learning Cards** page at `/cards` is a spaced-repetition study system built on the FSRS (Free Spaced Repetition Scheduler) algorithm. It keeps key facts from your papers fresh by scheduling each one just before you would forget it.

The page has two modes and picks the useful one for you: **review** (`?mode=review`) when cards are due, and **library** (`?mode=library`) when none are. There are no tabs — a **Review now** button takes you into a session, and the breadcrumb at the top of a session takes you back to the library.

Its settings live in **Settings → Research → Learning Cards**; see [Settings](settings.md).

---

## Library mode — `?mode=library`

<!-- screenshot: learning-cards-library -->

### StatsHeader

At the top of the library, the **StatsHeader** shows three summary statistics:

- **Total** — the total number of cards across all decks.
- **Due** — the number of cards currently due for review.
- **Streak** — your current consecutive daily review streak.

### DeckBrowser

Below the stats, **DeckBrowser** lists your decks. Each deck entry shows its name, the number of cards it contains, and how many are currently due. A **Start review** button for each deck launches a review session scoped to that deck.

### CardList

Selecting a deck lists its cards below the browser. Each card shows its front (question) face; clicking one expands it to show the answer as well.

### Generating cards

Click **Generate** to open the **GenerateCardsDialog**. Select one or more papers and a prompt style; the LLM pipeline generates a set of question-and-answer cards from the paper's chunks and adds them to a deck named after the paper.

### Creating a card manually

Click **New Card** to open the **CreateCardForm** and write a card by hand. Fill in the front (question), back (answer), and optionally assign it to an existing deck or create a new one.

### Review now

When cards are due, a **Review now (N)** button in the header shows the count and starts a session across all decks. **Start review** on a single deck scopes the session to that deck instead.

---

## Review mode — `?mode=review`

<!-- screenshot: learning-cards-session -->

### SessionProgressBar

A progress bar at the top of the review session shows how many cards you have reviewed out of the total due in this session.

### ReviewMode — card canvas and rating buttons

Cards are shown one at a time in a **card canvas**:

1. The **front** (question) of the card is shown first.
2. Click **Show answer** (or press Space) to reveal the **back** (answer).
3. Rate your recall using the rating buttons:

| Rating | Meaning |
|--------|---------|
| **Again** | You did not remember — the card is scheduled for very soon |
| **Hard** | You remembered with difficulty — interval shortened |
| **Good** | You remembered correctly — standard FSRS interval applied |
| **Easy** | You remembered easily — interval extended |

The FSRS algorithm uses your rating to compute the next review date for the card. Cards rated **Again** reappear within the current session.

### SessionComplete

When every due card in the session has been reviewed, a summary replaces the card canvas and a button returns you to the library.

---

## Offline review

Card ratings made while offline are stored in an **IndexedDB outbox**. When your connection is restored, queued ratings are automatically drained and synced to the server. The review session itself is fully functional offline; only the sync step requires a connection.

---

## Related pages

- [Paper Detail](paper-detail.md) — trigger card generation for a specific paper from the right-hand actions sidebar.
- [Analytics](analytics.md) — review activity and FSRS retention charts.
- [My Day & Home](home-my-day.md) — the Learning & focus chip shows today's due-card count and links here.
- [Settings](settings.md) — FSRS retention and learning steps, under §VI Research → Learning Cards.
