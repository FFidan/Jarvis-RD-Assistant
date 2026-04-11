import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WhyPopover } from '@/components/pulse/WhyPopover';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    explainPulseCard: vi.fn(),
  };
});

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

describe('WhyPopover', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not fetch until opened', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'matches your topic',
      signals: { topic_sim: 0.8 },
      llm_relevance: 9,
      llm_novelty: 7,
    });
    renderWithClient(<WhyPopover cardId={7} trigger={<button>Why?</button>} />);
    expect(explainPulseCard).not.toHaveBeenCalled();
  });

  it('renders skeleton while loading', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    // Never-resolving promise to keep us in loading state.
    vi.mocked(explainPulseCard).mockImplementation(
      () => new Promise(() => {}),
    );
    const user = userEvent.setup();
    renderWithClient(<WhyPopover cardId={7} trigger={<button>Why?</button>} />);
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(screen.getByTestId('why-popover-skeleton')).toBeInTheDocument();
    });
  });

  it('renders LLM reasoning when loaded', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'directly builds on your Neural ODE work',
      signals: { topic_sim: 0.8, author_overlap: 0.5 },
      llm_relevance: 9,
      llm_novelty: 7,
    });
    const user = userEvent.setup();
    renderWithClient(<WhyPopover cardId={7} trigger={<button>Why?</button>} />);
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(
        screen.getByText(/directly builds on your Neural ODE work/i),
      ).toBeInTheDocument();
    });
    expect(explainPulseCard).toHaveBeenCalledWith(7);
  });

  it('renders signal breakdown bars', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'r',
      signals: { topic_sim: 0.8, author_overlap: 0.25 },
      llm_relevance: 9,
      llm_novelty: 7,
    });
    const user = userEvent.setup();
    renderWithClient(<WhyPopover cardId={7} trigger={<button>Why?</button>} />);
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(screen.getByTestId('why-signal-topic_sim')).toBeInTheDocument();
      expect(screen.getByTestId('why-signal-author_overlap')).toBeInTheDocument();
    });
    // LLM scores should render.
    expect(screen.getByText(/relevance/i)).toBeInTheDocument();
    expect(screen.getByText(/novelty/i)).toBeInTheDocument();
  });
});
