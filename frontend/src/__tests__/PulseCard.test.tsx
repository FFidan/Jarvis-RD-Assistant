import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PulseCard } from '@/components/pulse/PulseCard';
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
  signals: { topic_sim: 0.8, author_overlap: 0.2 },
};

function renderCard(
  props: Partial<React.ComponentProps<typeof PulseCard>> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const onRate = props.onRate ?? vi.fn();
  const onOpen = props.onOpen;
  return {
    onRate,
    onOpen,
    ...render(
      <QueryClientProvider client={queryClient}>
        <PulseCard card={sampleCard} onRate={onRate} onOpen={onOpen} />
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

  it('renders up / down / save action buttons', () => {
    renderCard();
    expect(screen.getByRole('button', { name: /thumbs up/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /thumbs down/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /why/i })).toBeInTheDocument();
  });

  it('calls onRate with up when thumbs-up clicked', async () => {
    const user = userEvent.setup();
    const { onRate } = renderCard();
    await user.click(screen.getByRole('button', { name: /thumbs up/i }));
    expect(onRate).toHaveBeenCalledWith(42, 'up');
  });

  it('calls onRate with down when thumbs-down clicked', async () => {
    const user = userEvent.setup();
    const { onRate } = renderCard();
    await user.click(screen.getByRole('button', { name: /thumbs down/i }));
    expect(onRate).toHaveBeenCalledWith(42, 'down');
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
});
