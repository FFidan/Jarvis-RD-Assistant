import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WhyPopover } from '@/components/pulse/WhyPopover';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    explainPulseCard: vi.fn(),
  };
});

function renderWithClient(ui: React.ReactElement) {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    ui,
    { queryClient },
  );
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

  it('renders signal breakdown bars with human-readable labels', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'r',
      signals: { author_overlap: 0.25, emb: 0.8 },
      llm_relevance: 9,
      llm_novelty: 7,
    });
    const user = userEvent.setup();
    renderWithClient(<WhyPopover cardId={7} trigger={<button>Why?</button>} />);
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(screen.getByTestId('why-signal-author_overlap')).toBeInTheDocument();
      expect(screen.getByTestId('why-signal-emb')).toBeInTheDocument();
    });
    // Signal labels must be human-readable, not raw keys.
    expect(screen.getByText('Author overlap')).toBeInTheDocument();
    expect(screen.getByText('Semantic similarity')).toBeInTheDocument();
    // LLM scores should render.
    expect(screen.getByText(/relevance/i)).toBeInTheDocument();
    expect(screen.getByText(/novelty/i)).toBeInTheDocument();
  });

  it('closes on Escape key press', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'matches your topic',
      signals: {},
      llm_relevance: 9,
      llm_novelty: 7,
    });
    const user = userEvent.setup();
    renderWithClient(<WhyPopover cardId={7} trigger={<button>Why?</button>} />);
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(screen.getByText('Why this paper?')).toBeInTheDocument();
    });
    await user.keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByText('Why this paper?')).not.toBeInTheDocument();
    });
  });

  it('suppresses the per-card scoring-failed sentinel when the deck is degraded', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'LLM scoring failed',
      signals: { topic_sim: 0.8 },
      llm_relevance: null,
      llm_novelty: null,
    });
    const user = userEvent.setup();
    renderWithClient(<WhyPopover cardId={7} degraded trigger={<button>Why?</button>} />);
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(screen.getByText('Why this paper?')).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/AI scoring unavailable for this card/i),
    ).not.toBeInTheDocument();
  });

  it('still shows the per-card scoring-failed text for an isolated failure (not degraded)', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'LLM scoring failed',
      signals: { topic_sim: 0.8 },
      llm_relevance: null,
      llm_novelty: null,
    });
    const user = userEvent.setup();
    renderWithClient(<WhyPopover cardId={7} trigger={<button>Why?</button>} />);
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(
        screen.getByText(/AI scoring unavailable for this card/i),
      ).toBeInTheDocument();
    });
  });

  it('closes on outside click', async () => {
    const { explainPulseCard } = await import('@/lib/api');
    vi.mocked(explainPulseCard).mockResolvedValue({
      card_id: 7,
      reasoning: 'matches your topic',
      signals: {},
      llm_relevance: 9,
      llm_novelty: 7,
    });
    const user = userEvent.setup();
    renderWithClient(
      <div>
        <WhyPopover cardId={7} trigger={<button>Why?</button>} />
        <button>Outside</button>
      </div>,
    );
    await user.click(screen.getByText('Why?'));
    await waitFor(() => {
      expect(screen.getByText('Why this paper?')).toBeInTheDocument();
    });
    await user.click(screen.getByText('Outside'));
    await waitFor(() => {
      expect(screen.queryByText('Why this paper?')).not.toBeInTheDocument();
    });
  });
});
