import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PulseCard } from '@/components/pulse/PulseCard';
import * as api from '@/lib/api';
import type { PulseCardItem } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    explainPulseCard: vi.fn().mockResolvedValue({
      card_id: 1,
      reasoning: 'matches your topic',
      signals: {},
      llm_relevance: 8,
      llm_novelty: 6,
    }),
    trashAndRejectPaper: vi.fn().mockResolvedValue({ status: 'ok', paper_id: 42 }),
    submitFeedback: vi.fn().mockResolvedValue({}),
  };
});

const sampleCard: PulseCardItem = {
  card_id: 1,
  paper_id: 42,
  paper_title: 'Neural ODEs Revisited',
  paper_authors: ['Alice', 'Bob', 'Carol', 'Dave'],
  paper_url: 'https://arxiv.org/abs/1234.56789',
  rank: 1,
  score: 0.87,
  llm_relevance: 9,
  llm_novelty: 8,
  reasoning: 'Directly extends your prior work on continuous-depth models.',
  reasoning_verified: null,
  reasoning_confidence: null,
  signals: { topic_sim: 0.8, author_overlap: 0.2, l2_penalty: 0.1 },
};

function renderCard(
  props: Partial<React.ComponentProps<typeof PulseCard>> = {},
  cardOverrides: Partial<PulseCardItem> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onRate = props.onRate ?? vi.fn();
  const onOpen = props.onOpen;
  const card = { ...sampleCard, ...cardOverrides };
  return {
    onRate,
    onOpen,
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <PulseCard card={card} onRate={onRate} onOpen={onOpen} />
      </QueryClientProvider>,
    ),
  };
}

describe('PulseCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders rank, title, authors, and reasoning', () => {
    renderCard();
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('Neural ODEs Revisited')).toBeInTheDocument();
    // First 3 authors + ellipsis fallback when > 3.
    expect(screen.getByText(/Alice/)).toBeInTheDocument();
    expect(screen.getByText(/Bob/)).toBeInTheDocument();
    expect(screen.getByText(/Carol/)).toBeInTheDocument();
    expect(
      screen.getByText(/Directly extends your prior work/),
    ).toBeInTheDocument();
  });

  it('renders save and why action buttons', () => {
    renderCard();
    expect(screen.getByRole('button', { name: /save/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /why/i })).toBeInTheDocument();
  });

  it('renders FeedbackButtons (always — pulse-origin per spec §5.2)', () => {
    renderCard();
    // FeedbackButtons renders thumbs up/down with these aria-labels
    expect(screen.getByRole('button', { name: /recommend more like this/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /don't recommend like this/i })).toBeInTheDocument();
  });

  it('renders Trash & Reject button', () => {
    renderCard();
    expect(screen.getByRole('button', { name: /trash and reject/i })).toBeInTheDocument();
  });

  it('calls trashAndRejectPaper when Trash & Reject button clicked', async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByRole('button', { name: /trash and reject/i }));
    await waitFor(() => {
      expect(vi.mocked(api.trashAndRejectPaper)).toHaveBeenCalledWith(42);
    });
  });

  it('calls submitFeedback (positive) when FeedbackButtons thumbs-up clicked', async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByRole('button', { name: /recommend more like this/i }));
    await waitFor(() => {
      expect(vi.mocked(api.submitFeedback)).toHaveBeenCalledWith(42, { signal: 'positive', source: 'pulse_thumbs' });
    });
  });

  it('calls submitFeedback (negative) when FeedbackButtons thumbs-down clicked', async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByRole('button', { name: /don't recommend like this/i }));
    await waitFor(() => {
      expect(vi.mocked(api.submitFeedback)).toHaveBeenCalledWith(42, { signal: 'negative', source: 'pulse_thumbs' });
    });
  });

  it('calls onRate with save when save clicked', async () => {
    const user = userEvent.setup();
    const { onRate } = renderCard();
    await user.click(screen.getByRole('button', { name: /save/i }));
    expect(onRate).toHaveBeenCalledWith(42, 'save');
  });

  it('calls onOpen when card body clicked', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    renderCard({ onOpen });
    await user.click(screen.getByText('Neural ODEs Revisited'));
    expect(onOpen).toHaveBeenCalledWith(42);
  });

  it('shows WhyPopover when Why button clicked', async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByRole('button', { name: /why/i }));
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: /why this paper/i })).toBeInTheDocument();
    });
  });

  it('does not call onRate with up/down (legacy path removed)', () => {
    // The legacy onRate(id, 'up'/'down') buttons are gone; FeedbackButtons
    // takes over thumbs via submitFeedback. No button with the old aria-labels exists.
    renderCard();
    expect(screen.queryByRole('button', { name: /^thumbs up$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^thumbs down$/i })).not.toBeInTheDocument();
  });

  describe('reasoning verification badge', () => {
    it('renders green check icon when reasoning_verified is true', () => {
      renderCard({}, { reasoning_verified: true, reasoning_confidence: 'HIGH' });
      expect(screen.getByTestId('reasoning-verified-icon')).toBeInTheDocument();
      expect(screen.queryByTestId('reasoning-unverified-icon')).not.toBeInTheDocument();
    });

    it('renders amber warning icon when reasoning_verified is false', () => {
      renderCard({}, { reasoning_verified: false, reasoning_confidence: 'LOW' });
      expect(screen.getByTestId('reasoning-unverified-icon')).toBeInTheDocument();
      expect(screen.queryByTestId('reasoning-verified-icon')).not.toBeInTheDocument();
    });

    it('renders no verification icon when reasoning_verified is null', () => {
      renderCard({}, { reasoning_verified: null, reasoning_confidence: null });
      expect(screen.queryByTestId('reasoning-verified-icon')).not.toBeInTheDocument();
      expect(screen.queryByTestId('reasoning-unverified-icon')).not.toBeInTheDocument();
    });
  });
});
