import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TodaysPulseSection } from '@/components/my-day/sections/TodaysPulseSection';
import type { PulseCardItem, PulseDeck } from '@/types';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      jobs: {},
      activeAborts: {},
      hasRunning: () => false,
      isRunning: () => false,
      startJob: vi.fn(),
    }),
  ),
}));

vi.mock('@/lib/api', () => ({
  fetchPulseToday: vi.fn(),
  ratePulseCard: vi.fn(),
}));

const { fetchPulseToday } = await import('@/lib/api');

// ---------------------------------------------------------------------------
// Fixture builders
// ---------------------------------------------------------------------------

function makeCard(overrides: Partial<PulseCardItem> & { card_id: number; rank: number }): PulseCardItem {
  return {
    paper_id: overrides.card_id * 100,
    paper_title: `Paper ${overrides.rank}`,
    paper_authors: [],
    paper_url: null,
    score: 0.5,
    llm_relevance: null,
    llm_novelty: null,
    reasoning: null,
    reasoning_verified: null,
    reasoning_confidence: null,
    signals: {},
    user_state: null,
    ...overrides,
  };
}

function makeDeck(cards: PulseCardItem[]): PulseDeck {
  return {
    deck_id: 1,
    deck_date: '2026-05-02',
    card_count: cards.length,
    generated_at: '2026-05-02T08:00:00Z',
    cards,
    stats: {},
    degraded_reason: null,
  };
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TodaysPulseSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('TodaysPulseSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('displays contiguous display ranks #2-#5 even when server-set card ranks are gappy', async () => {
    // Simulate gappy server ranks — cards at positions 1, 4, 5, 8, 9 (as if earlier cards were dismissed)
    const cards: PulseCardItem[] = [
      makeCard({ card_id: 1, rank: 1 }),
      makeCard({ card_id: 4, rank: 4 }),
      makeCard({ card_id: 5, rank: 5 }),
      makeCard({ card_id: 8, rank: 8 }),
      makeCard({ card_id: 9, rank: 9 }),
    ];

    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck(cards));

    renderWithProviders();

    // The section renders tail cards (index 1+) with contiguous display ranks starting at 2
    expect(await screen.findByText('#2')).toBeInTheDocument();
    expect(screen.getByText('#3')).toBeInTheDocument();
    expect(screen.getByText('#4')).toBeInTheDocument();
    expect(screen.getByText('#5')).toBeInTheDocument();

    // Server-set gappy ranks must NOT appear
    expect(screen.queryByText('#8')).not.toBeInTheDocument();
    expect(screen.queryByText('#9')).not.toBeInTheDocument();
  });

  it('returns null when the deck has only one card (no tail to render)', async () => {
    const cards: PulseCardItem[] = [makeCard({ card_id: 1, rank: 1 })];

    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck(cards));

    const { container } = renderWithProviders();

    // Wait for query to resolve
    await new Promise((r) => setTimeout(r, 50));

    expect(container.firstChild).toBeNull();
  });

  it('shows skeleton rows while loading', () => {
    // fetchPulseToday never resolves — simulates pending state
    vi.mocked(fetchPulseToday).mockReturnValue(new Promise(() => {}));

    renderWithProviders();

    // SectionHeader renders "§ Today's pulse" — use a partial regex to match
    expect(screen.getByText(/Today's pulse/)).toBeInTheDocument();
  });

  it('shows error message when query fails', async () => {
    vi.mocked(fetchPulseToday).mockRejectedValue(new Error('Network error'));

    renderWithProviders();

    expect(await screen.findByText("Could not load today's pulse.")).toBeInTheDocument();
  });
});
