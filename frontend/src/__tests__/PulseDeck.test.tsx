import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { PulseDeck } from '@/components/my-day/PulseDeck';
import type { PulseDeck as PulseDeckType } from '@/types';

const mockStartJob = vi.fn();
const mockHasRunning = vi.fn().mockReturnValue(false);

vi.mock('@/stores/job-store', () => ({
  useJobStore: vi.fn((selector: (s: unknown) => unknown) =>
    selector({
      startJob: mockStartJob,
      hasRunning: mockHasRunning,
    }),
  ),
}));

vi.mock('@/lib/api', () => ({
  fetchPulseToday: vi.fn(),
  ratePulseCard: vi.fn(),
  explainPulseCard: vi.fn().mockResolvedValue({
    card_id: 1,
    reasoning: 'matches your topic',
    signals: {},
    llm_relevance: 8,
    llm_novelty: 6,
  }),
}));

const { fetchPulseToday, ratePulseCard } = await import(
  '@/lib/api'
);

function makeDeck(overrides: Partial<PulseDeckType> = {}): PulseDeckType {
  return {
    deck_id: 1,
    deck_date: '2026-04-11',
    card_count: 2,
    generated_at: '2026-04-11T08:00:00Z',
    stats: {},
    cards: [
      {
        card_id: 1,
        paper_id: 101,
        paper_title: 'Paper One',
        paper_authors: ['Alice'],
        paper_url: null,
        rank: 1,
        score: 0.9,
        llm_relevance: 8,
        llm_novelty: 7,
        reasoning: 'first reasoning',
        signals: {},
      },
      {
        card_id: 2,
        paper_id: 202,
        paper_title: 'Paper Two',
        paper_authors: ['Bob'],
        paper_url: null,
        rank: 2,
        score: 0.8,
        llm_relevance: 7,
        llm_novelty: 6,
        reasoning: 'second reasoning',
        signals: {},
      },
    ],
    ...overrides,
  };
}

function renderDeck() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<PulseDeck />} />
          <Route path="/paper/:paperId" element={<div data-testid="paper-detail">Paper Detail</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('PulseDeck', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows loading skeleton initially', () => {
    vi.mocked(fetchPulseToday).mockReturnValue(new Promise(() => {}));
    const { container } = renderDeck();
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows empty state when fetchPulseToday returns null', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
    renderDeck();
    expect(
      await screen.findByText(/no pulse deck yet today/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /generate now/i }),
    ).toBeInTheDocument();
  });

  it('calls startJob when generate button clicked in empty state', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchPulseToday).mockResolvedValue(null);
    mockStartJob.mockResolvedValue('test-job');
    renderDeck();
    const button = await screen.findByRole('button', {
      name: /generate now/i,
    });
    await user.click(button);
    await waitFor(() => {
      expect(mockStartJob).toHaveBeenCalledWith('pulse.generate', {});
    });
  });

  it('renders N PulseCard components when deck has N cards', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    renderDeck();
    expect(await screen.findByText('Paper One')).toBeInTheDocument();
    expect(screen.getByText('Paper Two')).toBeInTheDocument();
    // Header shows count
    expect(screen.getByText(/your pulse/i)).toBeInTheDocument();
    expect(screen.getByText(/2 papers/i)).toBeInTheDocument();
  });

  it('calls ratePulseCard when a card rating button clicked', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    vi.mocked(ratePulseCard).mockResolvedValue(undefined);
    renderDeck();
    await screen.findByText('Paper One');
    const thumbsUpButtons = screen.getAllByRole('button', {
      name: /thumbs up/i,
    });
    await user.click(thumbsUpButtons[0]);
    await waitFor(() => {
      expect(ratePulseCard).toHaveBeenCalledWith(101, 'up');
    });
  });

  it('shows error state when fetchPulseToday throws', async () => {
    vi.mocked(fetchPulseToday).mockRejectedValue(new Error('boom'));
    renderDeck();
    expect(await screen.findByText(/failed to load pulse/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('navigates to /paper/:paperId (singular) when a card is clicked', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    renderDeck();
    await screen.findByText('Paper One');
    // Click on the first card (click on the paper title)
    const paperOneLink = screen.getByText('Paper One');
    await user.click(paperOneLink);
    // Verify navigation to /paper/101
    await waitFor(() => {
      expect(screen.getByTestId('paper-detail')).toBeInTheDocument();
    });
  });
});
