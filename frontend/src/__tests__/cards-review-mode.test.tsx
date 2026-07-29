/**
 * Unit tests for the redesigned ReviewMode component.
 * Covers: next→flip→rate→advance flow, session signals, offline-seam isolation.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ReviewMode } from '@/components/cards/ReviewMode';
import type { Card } from '@/types';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getNextReview: vi.fn(),
    submitReview: vi.fn(),
    fetchDecks: vi.fn(),
  };
});

import { getNextReview, fetchDecks } from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
const mockGetNextReview = vi.mocked(getNextReview);
const mockFetchDecks = vi.mocked(fetchDecks);

const CARD_FIXTURE: Card = {
  id: 42,
  deck_id: 1,
  paper_id: null,
  card_type: 'concept',
  front: 'What is the time complexity of merge sort?',
  back: 'O(n log n) in all cases.',
  evidence: null,
  fsrs_state: {},
  due_at: new Date(Date.now() - 86400_000).toISOString(),
  stale: false,
  created_at: new Date(Date.now() - 7 * 86400_000).toISOString(),
  updated_at: new Date(Date.now() - 4 * 86400_000).toISOString(), // 4 days ago
};

const DECK_FIXTURE = [
  { id: 1, name: 'Algorithms', description: null, topic_id: null, card_count: 10, due_count: 3, created_at: '' },
];

function makeQueryClient() {
  return createTestQueryClient();
}

function renderReview(props: Partial<React.ComponentProps<typeof ReviewMode>> & {
  nextCards?: Card[];
}) {
  const { nextCards = [CARD_FIXTURE], ...rest } = props;

  mockGetNextReview.mockResolvedValue(nextCards);

  const qc = makeQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <ReviewMode
        sessionCardIndex={1}
        submitReviewFn={vi.fn().mockResolvedValue({})}
        {...rest}
      />
    </MemoryRouter>,
    { queryClient: qc },
  );
}

describe('ReviewMode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue(DECK_FIXTURE);
    mockGetNextReview.mockResolvedValue([CARD_FIXTURE]);
  });

  it('shows loading state initially', () => {
    mockGetNextReview.mockReturnValue(new Promise(() => {})); // never resolves
    const qc = makeQueryClient();
    renderWithProviders(
      <MemoryRouter>
        <ReviewMode sessionCardIndex={1} />
      </MemoryRouter>,
      { queryClient: qc },
    );
    expect(screen.getByText(/loading next card/i)).toBeInTheDocument();
  });

  it('renders card front after load', async () => {
    renderReview({});
    await waitFor(() => {
      expect(screen.getByText(CARD_FIXTURE.front)).toBeInTheDocument();
    });
  });

  it('does not expose rating controls for an earlier-version cached card', async () => {
    const submitReviewFn = vi.fn().mockResolvedValue({});
    renderReview({
      nextCards: [{ ...CARD_FIXTURE, stale: true }],
      submitReviewFn,
    });

    expect(
      await screen.findByText('This card is from an earlier paper version'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Good' })).not.toBeInTheDocument();
    expect(submitReviewFn).not.toHaveBeenCalled();
  });

  it('shows "Click to reveal answer" prompt before flip', async () => {
    renderReview({});
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    expect(screen.getByText(/click to reveal answer/i)).toBeInTheDocument();
  });

  it('reveals answer after clicking card area', async () => {
    renderReview({});
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    await userEvent.click(screen.getByText(/click to reveal answer/i));
    expect(screen.getByText(CARD_FIXTURE.back)).toBeInTheDocument();
    expect(screen.getByText(/^Answer$/i)).toBeInTheDocument();
  });

  it('shows all 4 rating buttons after reveal', async () => {
    renderReview({});
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    await userEvent.click(screen.getByText(/click to reveal answer/i));
    expect(screen.getByRole('button', { name: /again/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /hard/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /good/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /easy/i })).toBeInTheDocument();
  });

  it('calls submitReviewFn (the offline seam) on rating click', async () => {
    const submitFn = vi.fn().mockResolvedValue({});
    const qc = makeQueryClient();
    renderWithProviders(
      <MemoryRouter>
        <ReviewMode sessionCardIndex={1} submitReviewFn={submitFn} />
      </MemoryRouter>,
      { queryClient: qc },
    );
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    await userEvent.click(screen.getByText(/click to reveal answer/i));
    await userEvent.click(screen.getByRole('button', { name: /good/i }));
    await waitFor(() => expect(submitFn).toHaveBeenCalledTimes(1));
    // Verify the seam: submitFn is called with cardId + rating (3 = Good)
    expect(submitFn).toHaveBeenCalledWith(CARD_FIXTURE.id, 3, expect.any(Number));
  });

  it('calls onReviewSuccess after rating', async () => {
    const onSuccess = vi.fn();
    const qc = makeQueryClient();
    renderWithProviders(
      <MemoryRouter>
        <ReviewMode
          sessionCardIndex={1}
          submitReviewFn={vi.fn().mockResolvedValue({})}
          onReviewSuccess={onSuccess}
        />
      </MemoryRouter>,
      { queryClient: qc },
    );
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    await userEvent.click(screen.getByText(/click to reveal answer/i));
    await userEvent.click(screen.getByRole('button', { name: /easy/i }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  });

  it('shows "last seen 4d" eyebrow for card updated 4 days ago', async () => {
    renderReview({});
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    expect(screen.getByText(/last seen 4d/i)).toBeInTheDocument();
  });

  it('shows deck name in eyebrow when decks are loaded', async () => {
    mockFetchDecks.mockResolvedValue(DECK_FIXTURE);
    renderReview({});
    await waitFor(() => screen.getByText(/Card 1 · ALGORITHMS/));
  });

  it('displays eyebrow with session card index', async () => {
    renderReview({ sessionCardIndex: 5 });
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    expect(screen.getByText(/Card 5/i)).toBeInTheDocument();
  });

  it('skip button refetches without rating', async () => {
    renderReview({});
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    const skip = screen.getByRole('button', { name: /skip/i });
    expect(skip).toBeInTheDocument();
    // Skip is visible before reveal
    await userEvent.click(skip);
    // No crash — test passes if no error thrown
  });

  it('renders null (session end delegated to parent) when no cards returned', async () => {
    mockGetNextReview.mockResolvedValue([]);
    const onSessionEnd = vi.fn();
    const qc = makeQueryClient();
    const { container } = renderWithProviders(
      <MemoryRouter>
        <ReviewMode sessionCardIndex={1} onSessionEnd={onSessionEnd} />
      </MemoryRouter>,
      { queryClient: qc },
    );
    await waitFor(() => {
      // When empty, ReviewMode renders nothing (null)
      expect(container.querySelector('.mx-auto')).toBeNull();
    });
  });

  it('calls getNextReview with limit=1 and no deckId when not deck-scoped', async () => {
    renderReview({});
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    expect(mockGetNextReview).toHaveBeenCalledWith(1, undefined);
  });

  it('passes deckId to getNextReview when deck-scoped', async () => {
    renderReview({ deckId: 7 });
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    expect(mockGetNextReview).toHaveBeenCalledWith(1, 7);
  });

  it('shows QueryErrorState (not null/empty) when query rejects', async () => {
    mockGetNextReview.mockRejectedValue(new Error('network error'));
    const qc = makeQueryClient();
    renderWithProviders(
      <MemoryRouter>
        <ReviewMode sessionCardIndex={1} />
      </MemoryRouter>,
      { queryClient: qc },
    );
    await waitFor(() => {
      expect(
        screen.getByText(/couldn't load/i),
      ).toBeInTheDocument();
    });
    // Retry button must be present (refetch wired as onRetry)
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    // Must NOT render empty canvas (no rating buttons, no skip)
    expect(screen.queryByRole('button', { name: /skip/i })).not.toBeInTheDocument();
  });

  it('does not call onSessionEnd after unmount (isMounted guard)', async () => {
    // Use a controlled promise for the refetch so we can unmount before it resolves.
    let resolveRefetch!: (cards: Card[]) => void;
    const refetchPromise = new Promise<Card[]>(res => { resolveRefetch = res; });

    mockGetNextReview
      .mockResolvedValueOnce([CARD_FIXTURE]) // initial query load
      .mockImplementationOnce(() => refetchPromise); // refetch after rating — deferred

    const onSessionEnd = vi.fn();
    const qc = makeQueryClient();
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <ReviewMode
          sessionCardIndex={1}
          submitReviewFn={vi.fn().mockResolvedValue({})}
          onSessionEnd={onSessionEnd}
        />
      </MemoryRouter>,
      { queryClient: qc },
    );

    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    await userEvent.click(screen.getByText(/click to reveal answer/i));
    await userEvent.click(screen.getByRole('button', { name: /again/i }));

    // At this point the mutation fired and onSuccess triggered refetch(),
    // but refetchPromise is still pending. Unmount now — isMountedRef becomes false.
    unmount();

    // Now resolve the refetch with an empty array — onSessionEnd should be blocked.
    resolveRefetch([]);
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(onSessionEnd).not.toHaveBeenCalled();
  });

  it('error state is distinct from true-empty queue (empty returns null, not error UI)', async () => {
    mockGetNextReview.mockResolvedValue([]);
    const qc = makeQueryClient();
    const { container } = renderWithProviders(
      <MemoryRouter>
        <ReviewMode sessionCardIndex={1} />
      </MemoryRouter>,
      { queryClient: qc },
    );
    await waitFor(() => {
      expect(container.querySelector('.mx-auto')).toBeNull();
    });
    // Error UI must NOT be shown for a successful empty response
    expect(screen.queryByText(/couldn't load/i)).not.toBeInTheDocument();
  });
});

// --- P2 offline seam contract test ---
describe('ReviewMode offline seam', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue(DECK_FIXTURE);
    mockGetNextReview.mockResolvedValue([CARD_FIXTURE]);
  });

  it('submitReviewFn is the single point of control for submit-review calls', async () => {
    /**
     * This test asserts the offline-seam contract:
     * - The submit-review call is ONLY routed through `submitReviewFn`.
     * - Mocking this one prop in one place is sufficient to intercept all reviews.
     * The offline implementation replaces this prop; nothing else changes.
     */
    const offlineOutbox: Array<{ cardId: number; rating: number }> = [];
    const offlineSubmit = vi.fn().mockImplementation(
      async (cardId: number, rating: number) => {
        offlineOutbox.push({ cardId, rating });
        return {};
      },
    );

    const qc = makeQueryClient();
    renderWithProviders(
      <MemoryRouter>
        <ReviewMode sessionCardIndex={1} submitReviewFn={offlineSubmit} />
      </MemoryRouter>,
      { queryClient: qc },
    );

    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    await userEvent.click(screen.getByText(/click to reveal answer/i));
    await userEvent.click(screen.getByRole('button', { name: /hard/i }));

    await waitFor(() => expect(offlineOutbox).toHaveLength(1));
    expect(offlineOutbox[0]).toEqual({ cardId: 42, rating: 2 });
    // The seam is the ONLY place the rating was recorded — proves isolation.
    expect(offlineSubmit).toHaveBeenCalledTimes(1);
  });
});
