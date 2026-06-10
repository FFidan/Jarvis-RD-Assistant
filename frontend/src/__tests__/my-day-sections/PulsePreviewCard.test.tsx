import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PulsePreviewCard } from '@/components/my-day/PulsePreviewCard';
import { QUERY_KEYS } from '@/lib/query-keys';
import type { PulseDeck, PulseCardItem } from '@/types';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      startJob: vi.fn(),
      hasRunning: () => false,
    }),
  ),
}));

vi.mock('@/lib/api', () => ({
  fetchPulseToday: vi.fn(),
  ratePulseCard: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

const { fetchPulseToday, ratePulseCard } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Fixture builders
// ---------------------------------------------------------------------------

function makeCard(overrides: Partial<PulseCardItem> = {}): PulseCardItem {
  return {
    card_id: 1,
    paper_id: 101,
    paper_title: 'Test Paper',
    paper_authors: ['Alice'],
    paper_url: null,
    rank: 1,
    score: 0.9,
    llm_relevance: 8,
    llm_novelty: 7,
    reasoning: 'test reasoning',
    reasoning_verified: null,
    reasoning_confidence: null,
    signals: {},
    user_state: 'inbox' as const,
    tags: null,
    ...overrides,
  };
}

function makeDeck(overrides: Partial<PulseDeck> = {}): PulseDeck {
  return {
    deck_id: 1,
    deck_date: '2026-06-10',
    card_count: 1,
    generated_at: '2026-06-10T08:00:00Z',
    stats: {},
    degraded_reason: null,
    cards: [makeCard()],
    ...overrides,
  };
}

function renderCard(queryClient?: QueryClient) {
  const qc = queryClient ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<PulsePreviewCard />} />
            <Route path="/paper/:id" element={<div data-testid="paper-detail" />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PulsePreviewCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows skeleton while loading', () => {
    vi.mocked(fetchPulseToday).mockReturnValue(new Promise(() => {}));
    const { container } = renderCard();
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
  });

  it('shows error state when fetch fails', async () => {
    vi.mocked(fetchPulseToday).mockRejectedValue(new Error('network error'));
    renderCard();
    expect(await screen.findByText(/failed to load pulse/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('shows empty state when no deck exists', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
    renderCard();
    expect(await screen.findByText(/Generate your first Pulse/i)).toBeInTheDocument();
  });

  it('renders up to 3 preview cards from the deck', async () => {
    const cards = [
      makeCard({ card_id: 1, paper_id: 101, paper_title: 'Paper One', rank: 1 }),
      makeCard({ card_id: 2, paper_id: 102, paper_title: 'Paper Two', rank: 2 }),
      makeCard({ card_id: 3, paper_id: 103, paper_title: 'Paper Three', rank: 3 }),
      makeCard({ card_id: 4, paper_id: 104, paper_title: 'Paper Four', rank: 4 }),
    ];
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck({ card_count: 4, cards }));
    renderCard();
    expect(await screen.findByText('Paper One')).toBeInTheDocument();
    expect(screen.getByText('Paper Two')).toBeInTheDocument();
    expect(screen.getByText('Paper Three')).toBeInTheDocument();
    // 4th card must NOT appear in the preview
    expect(screen.queryByText('Paper Four')).not.toBeInTheDocument();
  });

  it('marks a card as rated (ratedCards) via usePulseRating onSuccess', async () => {
    const user = userEvent.setup();
    vi.mocked(ratePulseCard).mockResolvedValue();

    const PAPER_ID = 101;
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({ cards: [makeCard({ paper_id: PAPER_ID })] }),
    );

    renderCard();
    await screen.findByText('Test Paper');

    // Click the Save button — triggers the rating mutation via usePulseRating
    const saveButton = screen.getByRole('button', { name: /^save$/i });
    await user.click(saveButton);

    // After success, the card is in the rated set → PulseCard receives rated=true
    // (the button should become disabled/visually marked; checking ratePulseCard was called)
    await waitFor(() => {
      expect(ratePulseCard).toHaveBeenCalledWith(PAPER_ID, 'save');
    });
  });

  it('applies optimistic user_state update in the query cache when rating is "save"', async () => {
    vi.mocked(ratePulseCard).mockReturnValue(new Promise(() => {})); // never resolves

    const PAPER_ID = 101;
    const deck = makeDeck({ cards: [makeCard({ paper_id: PAPER_ID, user_state: 'inbox' })] });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), deck);

    const { qc: usedQc } = renderCard(qc);
    await screen.findByText('Test Paper');

    const user = userEvent.setup();
    const saveButton = screen.getByRole('button', { name: /^save$/i });
    await user.click(saveButton);

    // usePulseRating onMutate should have updated the cache optimistically
    await waitFor(() => {
      const cached = usedQc.getQueryData<PulseDeck>(QUERY_KEYS.pulse.today());
      expect(cached?.cards[0]?.user_state).toBe('to_read');
    });
  });

  it('rolls back optimistic update and shows toast on rating error', async () => {
    const { toast } = await import('sonner');
    vi.mocked(ratePulseCard).mockRejectedValue(new Error('server error'));

    const PAPER_ID = 101;
    const deck = makeDeck({ cards: [makeCard({ paper_id: PAPER_ID, user_state: 'inbox' })] });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    qc.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), deck);

    renderCard(qc);
    await screen.findByText('Test Paper');

    const user = userEvent.setup();
    const saveButton = screen.getByRole('button', { name: /^save$/i });
    await user.click(saveButton);

    // After rollback the cache must revert to the original user_state
    await waitFor(() => {
      const cached = qc.getQueryData<PulseDeck>(QUERY_KEYS.pulse.today());
      expect(cached?.cards[0]?.user_state).toBe('inbox');
    });

    // usePulseRating onError must fire toast.error
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('server error'));
  });
});
