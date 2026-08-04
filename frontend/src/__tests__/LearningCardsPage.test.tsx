/**
 * Regression-guard tests for LearningCardsPage v3 IA.
 * Covers: session / library mode routing, stats in Library, breadcrumb, DeckBrowser.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { LearningCardsPage } from '@/pages/LearningCardsPage';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getStats: vi.fn(),
    getNextReview: vi.fn(),
    fetchDecks: vi.fn(),
    fetchCards: vi.fn(),
    submitReview: vi.fn(),
  };
});

import { getStats, fetchDecks, submitReview } from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
const mockGetStats = vi.mocked(getStats);
const mockFetchDecks = vi.mocked(fetchDecks);
const mockSubmitReview = vi.mocked(submitReview);

const STATS_WITH_DUE = {
  total_cards: 20,
  due_now: 5,
  reviewed_today: 3,
  average_retention: 80.0,
  reviews_by_rating: {},
  streak_days: 2,
};

const STATS_ALL_DONE = {
  total_cards: 20,
  due_now: 0,
  reviewed_today: 10,
  average_retention: 90.0,
  reviews_by_rating: {},
  streak_days: 3,
};

const DECKS = [
  { id: 1, name: 'My Deck', description: null, topic_id: null, card_count: 5, due_count: 2, created_at: '' },
];

function renderPage(initialRoute = '/cards') {
  const qc = createTestQueryClient();
  // Mock fetch for review/next
  vi.spyOn(window, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : (input as Request).url;
    if (url.includes('/api/review/next')) {
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response('{}', { status: 200 });
  });
  return renderWithProviders(
    <MemoryRouter initialEntries={[initialRoute]}>
      <Routes>
        <Route path="/cards" element={<LearningCardsPage />} />
      </Routes>
    </MemoryRouter>,
    { queryClient: qc },
  );
}

describe('LearningCardsPage — mode routing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue([]);
    mockSubmitReview.mockResolvedValue({ card_id: 1, rating: 3, next_due_at: '', fsrs_state: {}, review_log_id: 1 });
  });

  it('shows review mode (breadcrumb) when due_now > 0', async () => {
    mockGetStats.mockResolvedValue(STATS_WITH_DUE);
    renderPage();
    await waitFor(() => expect(screen.getByText('Learn')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /flashcards/i })).toBeInTheDocument();
    expect(screen.getByText(/all decks · session/i)).toBeInTheDocument();
  });

  it('shows Library view when due_now === 0', async () => {
    mockGetStats.mockResolvedValue(STATS_ALL_DONE);
    renderPage();
    // Stats load triggers mode switch to Library (due_now=0 → Library default)
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /flashcards/i })).toBeInTheDocument(),
    );
  });

  it('shows Library view when ?mode=library in URL', async () => {
    mockGetStats.mockResolvedValue(STATS_WITH_DUE);
    renderPage('/cards?mode=library');
    await waitFor(() => expect(screen.getByRole('heading', { name: /flashcards/i })).toBeInTheDocument());
  });

  it('navigates to Library when breadcrumb Flashcards clicked', async () => {
    mockGetStats.mockResolvedValue(STATS_WITH_DUE);
    renderPage();
    await waitFor(() => screen.getByRole('button', { name: /flashcards/i }));
    await userEvent.click(screen.getByRole('button', { name: /flashcards/i }));
    await waitFor(() => expect(screen.getByRole('heading', { name: /flashcards/i })).toBeInTheDocument());
  });
});

describe('LearningCardsPage — Library view content', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetStats.mockResolvedValue(STATS_ALL_DONE);
    mockFetchDecks.mockResolvedValue(DECKS);
    mockSubmitReview.mockResolvedValue({ card_id: 1, rating: 3, next_due_at: '', fsrs_state: {}, review_log_id: 1 });
  });

  it('renders Generate and New Card buttons in Library', async () => {
    renderPage();
    await waitFor(() => screen.getByRole('heading', { name: /flashcards/i }));
    expect(screen.getByRole('button', { name: /generate/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /new card/i })).toBeInTheDocument();
  });

  it('renders StatsHeader tiles in Library view', async () => {
    renderPage();
    await waitFor(() => screen.getByRole('heading', { name: /flashcards/i }));
    // StatsHeader renders after stats load
    await waitFor(() => expect(screen.getByText('Total Cards')).toBeInTheDocument());
    expect(screen.getByText('Due Now')).toBeInTheDocument();
    expect(screen.getByText('Reviewed Today')).toBeInTheDocument();
  });

  it('does NOT render StatsHeader in review session mode', async () => {
    mockGetStats.mockResolvedValue(STATS_WITH_DUE);
    renderPage();
    await waitFor(() => screen.getByText(/all decks · session/i));
    expect(screen.queryByText('Total Cards')).toBeNull();
  });

  it('shows deck grid in Library', async () => {
    renderPage();
    await waitFor(() => screen.getByRole('heading', { name: /flashcards/i }));
    await waitFor(() => expect(screen.getByText('My Deck')).toBeInTheDocument());
  });

  it('does not show old Review/Browse tabs', async () => {
    renderPage();
    await waitFor(() => screen.getByRole('heading', { name: /flashcards/i }));
    // Old tab structure is gone
    expect(screen.queryByRole('tab', { name: /browse/i })).toBeNull();
  });
});

describe('LearningCardsPage — stats failure (StatsHeader)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue([]);
    mockSubmitReview.mockResolvedValue({ card_id: 1, rating: 3, next_due_at: '', fsrs_state: {}, review_log_id: 1 });
  });

  it('surfaces a stats error with Retry instead of silently hiding the header', async () => {
    mockGetStats.mockRejectedValue(new Error('stats down'));
    renderPage('/cards?mode=library');

    // Failed stats render an explicit error, not a blank header.
    expect(await screen.findByText("Couldn't load your stats.")).toBeInTheDocument();
    expect(screen.queryByText('Total Cards')).toBeNull();

    // Retry re-invokes the query and restores the tiles.
    mockGetStats.mockResolvedValue(STATS_ALL_DONE);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('Total Cards')).toBeInTheDocument());
    expect(screen.queryByText("Couldn't load your stats.")).toBeNull();
  });
});

describe('LearningCardsPage — deck list failure in review mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetStats.mockResolvedValue(STATS_WITH_DUE);
    mockSubmitReview.mockResolvedValue({ card_id: 1, rating: 3, next_due_at: '', fsrs_state: {}, review_log_id: 1 });
  });

  it('shows a deck-name error under the breadcrumb when decks fail to load', async () => {
    mockFetchDecks.mockRejectedValue(new Error('network down'));
    renderPage();
    await waitFor(() => expect(screen.getByText(/all decks · session/i)).toBeInTheDocument());
    expect(await screen.findByText('Failed to load deck names.')).toBeInTheDocument();
  });

  it('shows no deck-name error when decks load empty', async () => {
    mockFetchDecks.mockResolvedValue([]);
    renderPage();
    await waitFor(() => expect(screen.getByText(/all decks · session/i)).toBeInTheDocument());
    expect(screen.queryByText('Failed to load deck names.')).toBeNull();
  });
});

describe('LearningCardsPage — breadcrumb and progress', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetStats.mockResolvedValue(STATS_WITH_DUE);
    mockFetchDecks.mockResolvedValue([]);
    mockSubmitReview.mockResolvedValue({ card_id: 1, rating: 3, next_due_at: '', fsrs_state: {}, review_log_id: 1 });
  });

  it('renders progress bar in review mode', async () => {
    renderPage();
    await waitFor(() => screen.getByText(/progress/i));
    expect(screen.getByRole('progressbar')).toBeInTheDocument();
  });

  it('renders PROGRESS label (uppercase)', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(/progress/i)).toBeInTheDocument());
  });

  it('shows 0 / N counter on fresh session after stats load', async () => {
    renderPage();
    // Progress counter shows reviewed/total after stats load with due_now=5
    await waitFor(() => expect(screen.getByText(/0 \/ 5/)).toBeInTheDocument());
  });
});

describe('LearningCardsPage — session end', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue([]);
    mockSubmitReview.mockResolvedValue({ card_id: 1, rating: 3, next_due_at: '', fsrs_state: {}, review_log_id: 1 });
  });

  it('shows "Review now" CTA in Library when cards are due', async () => {
    mockGetStats.mockResolvedValue({ ...STATS_ALL_DONE, due_now: 3 });
    renderPage('/cards?mode=library');
    // Wait for stats to resolve and CTA to appear
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /review now/i })).toBeInTheDocument(),
    );
  });
});

describe('LearningCardsPage — deck-scope bleed (F8)', () => {
  /**
   * Regression guard: after a deck-scoped session, starting a global review
   * via "Review now" must NOT retain the previous sessionDeckId.
   *
   * Observable: when sessionDeckId is null the breadcrumb reads "All decks · session".
   * If bleed occurs, it would read "My Deck · session" (the prior deck name).
   */
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetStats.mockResolvedValue({ ...STATS_WITH_DUE, due_now: 3 });
    mockFetchDecks.mockResolvedValue(DECKS);
    mockSubmitReview.mockResolvedValue({ card_id: 1, rating: 3, next_due_at: '', fsrs_state: {}, review_log_id: 1 });
  });

  it('does not bleed sessionDeckId when navigateToReview is called globally', async () => {
    renderPage('/cards?mode=library');

    // Wait for Library to render with a deck.
    await waitFor(() => expect(screen.getByRole('heading', { name: /flashcards/i })).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText('My Deck')).toBeInTheDocument());

    // Start a deck-scoped review session (simulates handleStartReview(1)).
    // DeckBrowser renders a Play button with title "Start review — N cards due".
    const startReviewBtn = screen.getByTitle(/start review/i);
    await userEvent.click(startReviewBtn);

    // We are now in review mode scoped to deck 1 — breadcrumb should show deck name.
    await waitFor(() => expect(screen.getByText(/my deck · session/i)).toBeInTheDocument());

    // Navigate back to Library via breadcrumb.
    await userEvent.click(screen.getByRole('button', { name: /flashcards/i }));

    // Library shows "Review now" CTA (due_now=3).
    await waitFor(() => expect(screen.getByRole('button', { name: /review now/i })).toBeInTheDocument());

    // Click "Review now" — this calls navigateToReview() with no deck argument.
    await userEvent.click(screen.getByRole('button', { name: /review now/i }));

    // ASSERTION: breadcrumb must show "All decks · session" — not "My Deck · session".
    // This proves sessionDeckId was reset to null (no bleed from the prior deck session).
    await waitFor(() => expect(screen.getByText(/all decks · session/i)).toBeInTheDocument());
    expect(screen.queryByText(/my deck · session/i)).toBeNull();
  });
});
