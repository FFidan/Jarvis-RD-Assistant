import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PulseDeck } from '@/components/my-day/PulseDeck';
import type { PulseDeck as PulseDeckType, PulseStats } from '@/types';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

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
  fetchPulseStats: vi.fn().mockResolvedValue({ has_learned_model: true }),
  ratePulseCard: vi.fn(),
  explainPulseCard: vi.fn().mockResolvedValue({
    card_id: 1,
    reasoning: 'matches your topic',
    signals: {},
    llm_relevance: 8,
    llm_novelty: 6,
  }),
}));

const { fetchPulseToday, fetchPulseStats } = await import(
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
        reasoning_verified: null,
        reasoning_confidence: null,
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
        reasoning_verified: null,
        reasoning_confidence: null,
        signals: {},
      },
    ],
    ...overrides,
  };
}

function makeStats(overrides: Partial<PulseStats> = {}): PulseStats {
  return {
    window_days: 30,
    decks_generated: 1,
    avg_candidates: null,
    avg_llm_calls: null,
    avg_duration_s: null,
    last_run_at: null,
    last_error: null,
    degraded_reason: null,
    has_learned_model: true,
    ...overrides,
  };
}

function renderDeck() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<PulseDeck />} />
        <Route path="/paper/:paperId" element={<div data-testid="paper-detail">Paper Detail</div>} />
      </Routes>
    </MemoryRouter>,
    { queryClient },
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

  it('shows the basic-ranking caption when has_learned_model is false', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    vi.mocked(fetchPulseStats).mockResolvedValueOnce(makeStats({ has_learned_model: false }));
    renderDeck();
    await screen.findByText('Paper One');
    expect(
      screen.getByText(/basic ranking \(learning from your feedback\)/i),
    ).toBeInTheDocument();
  });

  it('hides the basic-ranking caption when has_learned_model is true', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    vi.mocked(fetchPulseStats).mockResolvedValueOnce(makeStats({ has_learned_model: true }));
    renderDeck();
    await screen.findByText('Paper One');
    expect(
      screen.queryByText(/basic ranking \(learning from your feedback\)/i),
    ).not.toBeInTheDocument();
  });

  it('shows degraded reason and source diagnostics for an empty generated deck', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({
        card_count: 0,
        cards: [],
        degraded_reason: 'No Pulse candidates returned; arXiv rate limit reached.',
        stats: {
          source_diagnostics: {
            arxiv: {
              status: 'rate_limit',
              message: 'arxiv returned HTTP 429',
              status_code: 429,
              retry_after_s: 60,
              settings_hint: null,
            },
            openalex: {
              status: 'unconfigured',
              message: 'OpenAlex requires an API key.',
              status_code: null,
              retry_after_s: null,
              settings_hint: 'Set OPENALEX_API_KEY for OpenAlex access.',
            },
          },
        },
      }),
    );

    renderDeck();

    expect(await screen.findByText(/arxiv rate limit reached/i)).toBeInTheDocument();
    expect(screen.getByText(/arxiv returned HTTP 429/i)).toBeInTheDocument();
    expect(screen.getByText(/set OPENALEX_API_KEY/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Regenerate' })).toBeInTheDocument();
    // A degraded run with zero cards still shows the empty-state copy under the banner,
    // rather than leaving a blank container below it.
    expect(screen.getByText(/no cards yet/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /regenerate deck/i })).toBeInTheDocument();
  });

  it('offers a regenerate CTA when the deck has no cards and no degraded reason', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({ card_count: 0, cards: [] }),
    );
    renderDeck();
    expect(await screen.findByText(/no cards yet/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /regenerate deck/i }),
    ).toBeInTheDocument();
  });

  it('bounds source diagnostics and shows the hidden warning count', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({
        card_count: 0,
        cards: [],
        degraded_reason: 'No Pulse candidates returned.',
        stats: {
          source_diagnostics: {
            arxiv: { status: 'rate_limit', message: 'arxiv 429' },
            openalex: { status: 'unconfigured', message: 'openalex missing key' },
            semantic_scholar: { status: 'rate_limit', message: 's2 429' },
            pubmed: { status: 'rate_limit', message: 'pubmed 429' },
            local: { status: 'unsupported', message: 'local unsupported' },
          },
        },
      }),
    );

    renderDeck();

    expect(await screen.findByText(/arxiv 429/i)).toBeInTheDocument();
    expect(screen.getByText(/openalex missing key/i)).toBeInTheDocument();
    expect(screen.getByText(/s2 429/i)).toBeInTheDocument();
    expect(screen.queryByText(/pubmed 429/i)).not.toBeInTheDocument();
    expect(screen.getByText(/\+2 more source warnings/i)).toBeInTheDocument();
  });

  it('shows error state when fetchPulseToday throws', async () => {
    vi.mocked(fetchPulseToday).mockRejectedValue(new Error('boom'));
    renderDeck();
    expect(await screen.findByText(/couldn't load your recommendations/i)).toBeInTheDocument();
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

  it('shows regenerate button in header when deck has cards', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    renderDeck();
    await screen.findByText('Paper One');
    expect(screen.getByRole('button', { name: /regenerate/i })).toBeInTheDocument();
  });

  it('disables header regenerate button and shows spinner while generating', async () => {
    mockHasRunning.mockReturnValue(true);
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    renderDeck();
    await screen.findByText('Paper One');
    const buttons = screen.getAllByRole('button', { name: /generating/i });
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons[0]).toBeDisabled();
    mockHasRunning.mockReturnValue(false);
  });

  it('shows regenerate CTA when all cards have AI scoring unavailable', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({
        cards: [
          {
            card_id: 1,
            paper_id: 101,
            paper_title: 'Paper One',
            paper_authors: ['Alice'],
            paper_url: null,
            rank: 1,
            score: 0.9,
            llm_relevance: null,
            llm_novelty: null,
            reasoning: 'LLM scoring failed',
            reasoning_verified: null,
            reasoning_confidence: null,
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
            llm_relevance: null,
            llm_novelty: null,
            reasoning: 'LLM scoring failed',
            reasoning_verified: null,
            reasoning_confidence: null,
            signals: {},
          },
        ],
      }),
    );
    renderDeck();
    await screen.findByText('Paper One');
    expect(
      screen.getByText(/AI scoring is unavailable for all cards/i),
    ).toBeInTheDocument();
    const regenerateButtons = screen.getAllByRole('button', { name: /regenerate/i });
    expect(regenerateButtons.length).toBeGreaterThanOrEqual(2);
  });

  it('does not show all-scoring-unavailable banner when only some cards lack scoring', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({
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
            reasoning: 'LLM scoring failed',
            reasoning_verified: null,
            reasoning_confidence: null,
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
            reasoning: 'valid reasoning',
            reasoning_verified: null,
            reasoning_confidence: null,
            signals: {},
          },
        ],
      }),
    );
    renderDeck();
    await screen.findByText('Paper One');
    expect(
      screen.queryByText(/AI scoring is unavailable for all cards/i),
    ).not.toBeInTheDocument();
  });

  it('shows ONE calm deck banner and suppresses per-card scoring-failed text when degraded', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({
        degraded_reason:
          'AI relevance scoring is temporarily unavailable — showing basic ranking.',
        cards: [
          {
            card_id: 1,
            paper_id: 101,
            paper_title: 'Paper One',
            paper_authors: ['Alice'],
            paper_url: null,
            rank: 1,
            score: 0.9,
            llm_relevance: null,
            llm_novelty: null,
            reasoning: 'LLM scoring failed',
            reasoning_verified: null,
            reasoning_confidence: null,
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
            llm_relevance: null,
            llm_novelty: null,
            reasoning: 'LLM scoring failed',
            reasoning_verified: null,
            reasoning_confidence: null,
            signals: {},
          },
        ],
      }),
    );
    renderDeck();
    await screen.findByText('Paper One');

    // The single calm deck-level banner renders the backend's basic-ranking reason.
    expect(
      screen.getByText(/showing basic ranking/i),
    ).toBeInTheDocument();
    // No per-card "AI scoring unavailable for this card" text on any card.
    expect(
      screen.queryByText(/AI scoring unavailable for this card/i),
    ).not.toBeInTheDocument();
    // The alarming all-cards banner is not shown when a degraded reason already explains it.
    expect(
      screen.queryByText(/AI scoring is unavailable for all cards/i),
    ).not.toBeInTheDocument();
  });

  it('still shows per-card scoring-failed text when the deck is NOT degraded', async () => {
    vi.mocked(fetchPulseToday).mockResolvedValue(
      makeDeck({
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
            reasoning: 'LLM scoring failed',
            reasoning_verified: null,
            reasoning_confidence: null,
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
            reasoning: 'valid reasoning',
            reasoning_verified: null,
            reasoning_confidence: null,
            signals: {},
          },
        ],
      }),
    );
    renderDeck();
    await screen.findByText('Paper One');
    // Isolated single-card failure (no deck-level degraded_reason) keeps its per-card text.
    expect(
      screen.getByText(/AI scoring unavailable for this card/i),
    ).toBeInTheDocument();
  });

  it('only passes savePending=true to the targeted card while a mutation is in flight', async () => {
    const { ratePulseCard } = await import('@/lib/api');
    // Return a never-settling promise to keep the mutation in-flight
    vi.mocked(ratePulseCard).mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    vi.mocked(fetchPulseToday).mockResolvedValue(makeDeck());
    renderDeck();

    await screen.findByText('Paper One');
    await screen.findByText('Paper Two');

    // Both Save buttons should be enabled before any mutation
    const saveButtonsBefore = screen.getAllByRole('button', { name: /^save$/i });
    expect(saveButtonsBefore).toHaveLength(2);
    expect(saveButtonsBefore[0]).not.toBeDisabled();
    expect(saveButtonsBefore[1]).not.toBeDisabled();

    // Click Save on Paper One — mutation starts, stays in flight
    const card1SaveButton = saveButtonsBefore[0];
    if (!card1SaveButton) throw new Error('Paper One save button not found');
    await user.click(card1SaveButton);

    // Paper Two's Save button must NOT be disabled while Paper One is saving
    await waitFor(() => {
      const buttons = screen.getAllByRole('button', { name: /^save$/i });
      const card2SaveButton = buttons[buttons.length - 1];
      expect(card2SaveButton).not.toBeDisabled();
    });
  });
});
