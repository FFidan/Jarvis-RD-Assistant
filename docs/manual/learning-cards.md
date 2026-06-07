<!-- verified-against-UI: 2026-06-06 | routes: /cards, /cards?mode=review, /cards?mode=library -->

# Learning Cards

_This area is evolving; verified 2026-06-06._

The **Learning Cards** page at `/cards` is a spaced-repetition study system built on the FSRS (Free Spaced Repetition Scheduler) algorithm. It keeps key facts from your research papers fresh with scientifically-spaced review intervals.

The page opens in **review mode** (`?mode=review`) when you have cards due for review; otherwise it defaults to **library mode** (`?mode=library`). You can switch between modes using tabs at the top of the page.

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

The card list below the deck browser shows all cards, or the cards within a selected deck. Each card shows its front (question) face. Clicking a card expands it to show both front and back.

### Generating cards

Click **Generate** to open the **GenerateCardsDialog**. Select one or more papers and a prompt style; the LLM pipeline generates a set of question-and-answer cards from the paper's chunks and adds them to a deck named after the paper.

### Creating a card manually

Click **New Card** to open the **CreateCardForm** and write a card by hand. Fill in the front (question), back (answer), and optionally assign it to an existing deck or create a new one.

### Review now button

If cards are due, a **"Review now (N)"** button appears showing the count. Clicking it switches to review mode.

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

When all due cards in the session have been reviewed, the **SessionComplete** screen shows a summary: cards reviewed, ratings breakdown, and estimated next review dates. A button returns you to the library or to the full `/cards` page.

---

## Offline review

Card ratings made while offline are stored in an **IndexedDB outbox**. When your connection is restored, queued ratings are automatically drained and synced to the server. The review session itself is fully functional offline; only the sync step requires a connection.

---

## Related pages

- [Paper Detail](paper-detail.md) — trigger card generation for a specific paper from the right-hand actions sidebar.
- [Analytics](analytics.md) — review activity and FSRS retention charts.
- [My Day & Home](home-my-day.md) — LearningFocusSection shows today's due-card count with a link here.
