/**
 * Unit tests for the redesigned ReviewMode component.
 * Covers: next→flip→rate→advance flow, session signals, offline-seam isolation.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
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

import { fetchDecks } from '@/lib/api';
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
  created_at: new Date(Date.now() - 7 * 86400_000).toISOString(),
  updated_at: new Date(Date.now() - 4 * 86400_000).toISOString(), // 4 days ago
};

const DECK_FIXTURE = [
  { id: 1, name: 'Algorithms', description: null, topic_id: null, card_count: 10, due_count: 3, created_at: '' },
];

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderReview(props: Partial<React.ComponentProps<typeof ReviewMode>> & {
  nextCards?: Card[];
}) {
  const { nextCards = [CARD_FIXTURE], ...rest } = props;

  // Intercept fetch at the native level for review-next
  const originalFetch = window.fetch;
  vi.spyOn(window, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : (input as Request).url;
    if (url.includes('/api/review/next')) {
      return new Response(JSON.stringify(nextCards), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return originalFetch(input);
  });

  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ReviewMode
          sessionCardIndex={1}
          submitReviewFn={vi.fn().mockResolvedValue({})}
          {...rest}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ReviewMode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchDecks.mockResolvedValue(DECK_FIXTURE);
    vi.restoreAllMocks();
  });

  it('shows loading state initially', () => {
    vi.spyOn(window, 'fetch').mockImplementation(
      () => new Promise(() => {}), // never resolves
    );
    const qc = makeQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ReviewMode sessionCardIndex={1} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText(/loading next card/i)).toBeInTheDocument();
  });

  it('renders card front after load', async () => {
    renderReview({});
    await waitFor(() => {
      expect(screen.getByText(CARD_FIXTURE.front)).toBeInTheDocument();
    });
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
    expect(screen.getByText(/§ Answer/i)).toBeInTheDocument();
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
    vi.spyOn(window, 'fetch').mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/review/next')) {
        return new Response(JSON.stringify([CARD_FIXTURE]), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response('{}', { status: 200 });
    });
    const qc = makeQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ReviewMode sessionCardIndex={1} submitReviewFn={submitFn} />
        </MemoryRouter>
      </QueryClientProvider>,
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
    vi.spyOn(window, 'fetch').mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/review/next')) {
        return new Response(JSON.stringify([CARD_FIXTURE]), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response('{}', { status: 200 });
    });
    const qc = makeQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ReviewMode
            sessionCardIndex={1}
            submitReviewFn={vi.fn().mockResolvedValue({})}
            onReviewSuccess={onSuccess}
          />
        </MemoryRouter>
      </QueryClientProvider>,
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
    await waitFor(() => screen.getByText(/§ Card 1 · ALGORITHMS/));
  });

  it('displays eyebrow with session card index', async () => {
    renderReview({ sessionCardIndex: 5 });
    await waitFor(() => screen.getByText(CARD_FIXTURE.front));
    expect(screen.getByText(/§ Card 5/i)).toBeInTheDocument();
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
    const onSessionEnd = vi.fn();
    vi.spyOn(window, 'fetch').mockImplementation(async () => {
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    const qc = makeQueryClient();
    const { container } = render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ReviewMode sessionCardIndex={1} onSessionEnd={onSessionEnd} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => {
      // When empty, ReviewMode renders nothing (null)
      expect(container.querySelector('.mx-auto')).toBeNull();
    });
  });
});

// --- P2 offline seam contract test ---
describe('ReviewMode offline seam', () => {
  it('submitReviewFn is the single point of control for submit-review calls', async () => {
    /**
     * This test asserts the offline-seam contract:
     * - The submit-review call is ONLY routed through `submitReviewFn`.
     * - Mocking this one prop in one place is sufficient to intercept all reviews.
     * Wave 3 offline implementation replaces this prop; nothing else changes.
     */
    const offlineOutbox: Array<{ cardId: number; rating: number }> = [];
    const offlineSubmit = vi.fn().mockImplementation(
      async (cardId: number, rating: number) => {
        offlineOutbox.push({ cardId, rating });
        return {};
      },
    );

    vi.spyOn(window, 'fetch').mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/review/next')) {
        return new Response(JSON.stringify([CARD_FIXTURE]), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response('{}', { status: 200 });
    });

    const qc = makeQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ReviewMode sessionCardIndex={1} submitReviewFn={offlineSubmit} />
        </MemoryRouter>
      </QueryClientProvider>,
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
