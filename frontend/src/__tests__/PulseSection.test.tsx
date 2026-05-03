import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PulseSection } from '@/components/settings/PulseSection';
import type { PulseStats } from '@/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: '', value: null }),
    fetchPulseStats: vi.fn(),
    fetchPulseDebug: vi.fn().mockResolvedValue({
      deck_date: '2026-04-17',
      card_count: 5,
      degraded_reason: null,
      source_counts: { arxiv: 12, pubmed: 8 },
      topic_embeddings: [{ key: 'topic.1.embedding', dim: 768, ok: true, non_null: true }],
      top_cards: [],
      classifier_available: false,
      classifier_sample_count: null,
      classifier_feature_names: [],
      classifier_auc: null,
      classifier_auc_degradation_reason: null,
      classifier_degradation_reason: null,
    }),
    createJob: vi.fn().mockResolvedValue({ job_id: 'pulse-job-1', status: 'queued' }),
    listJobs: vi.fn().mockResolvedValue([]),
  };
});

const { fetchConfig, fetchPulseStats, setConfig } = await import('@/lib/api');

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PulseSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('PulseSection', () => {
  const baseStats: PulseStats = {
    window_days: 1,
    decks_generated: 3,
    avg_candidates: 42,
    avg_llm_calls: 15,
    avg_duration_s: 12.3,
    last_run_at: '2026-04-10T04:00:00Z',
    last_error: null,
    degraded_reason: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.enabled', value: false },
      { key: 'pulse.cron', value: '0 4 * * *' },
      { key: 'pulse.deck_size', value: 10 },
      { key: 'pulse.stage2_top_k', value: 40 },
    ]);
    vi.mocked(fetchPulseStats).mockResolvedValue(baseStats);
  });

  it('renders the Pulse enable toggle', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: /pulse/i })).toBeInTheDocument();
    });
  });

  it('calls setConfig with pulse.enabled when toggled', async () => {
    const user = userEvent.setup();
    renderSection();
    const toggle = await screen.findByRole('switch', { name: /pulse/i });
    await user.click(toggle);
    await waitFor(() => {
      expect(vi.mocked(setConfig)).toHaveBeenCalledWith('pulse.enabled', true);
    });
  });

  it('renders last-run status with stats data', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/decks generated/i)).toBeInTheDocument();
    });
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  // ── Badge state tests ──────────────────────────────────────────────────────

  it('shows Failed badge (red) when last_error is set', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      decks_generated: 0,
      last_run_at: null,
      last_error: 'scoring pipeline exploded',
      degraded_reason: null,
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('Failed')).toBeInTheDocument();
    });
    // The error message is also shown inline in the stats section
    expect(screen.getByText(/scoring pipeline exploded/i)).toBeInTheDocument();
  });

  it('shows Degraded badge (amber) when degraded_reason is set and last_error is null', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      last_error: null,
      degraded_reason: 'optional Pulse Phase 2 signals unavailable: no networkx',
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('Degraded')).toBeInTheDocument();
    });
    // The tooltip trigger element should carry the badge text
    const badge = screen.getByText('Degraded');
    expect(badge).toBeInTheDocument();
  });

  it('Degraded badge tooltip content includes the degraded_reason text', async () => {
    const degradedMsg = 'optional Pulse Phase 2 signals unavailable: no networkx';
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      last_error: null,
      degraded_reason: degradedMsg,
    });
    const user = userEvent.setup();
    renderSection();
    const badge = await screen.findByText('Degraded');
    await user.hover(badge);
    // Radix renders tooltip content twice (visible div + hidden aria span), use getAllByText
    await waitFor(() => {
      const matches = screen.getAllByText(degradedMsg);
      expect(matches.length).toBeGreaterThan(0);
    });
  });

  it('shows OK badge (green) when both last_error and degraded_reason are null', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      ...baseStats,
      last_error: null,
      degraded_reason: null,
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('OK')).toBeInTheDocument();
    });
  });

  // ── Conditional-signal gate tooltip tests ─────────────────────────────────

  it('renders gate-tooltip trigger wrapper for the classifier slider', async () => {
    renderSection();
    await waitFor(() => {
      expect(
        screen.getByTestId('gate-tooltip-trigger-classifier'),
      ).toBeInTheDocument();
    });
  });

  it('renders gate-tooltip trigger wrapper for citation_pagerank slider', async () => {
    renderSection();
    await waitFor(() => {
      expect(
        screen.getByTestId('gate-tooltip-trigger-citation_pagerank'),
      ).toBeInTheDocument();
    });
  });

  it('gate tooltip for classifier contains sklearn + ratings requirement text', async () => {
    const user = userEvent.setup();
    renderSection();
    const trigger = await screen.findByTestId('gate-tooltip-trigger-classifier');
    await user.hover(trigger);
    // Radix may render tooltip text in multiple nodes (visible + hidden aria span)
    await waitFor(() => {
      expect(screen.getAllByText(/scikit-learn/i).length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText(/30 Pulse ratings/i).length).toBeGreaterThan(0);
  });

  it('gate tooltip for citation_pagerank contains networkx + paper_citations requirement text', async () => {
    const user = userEvent.setup();
    renderSection();
    const trigger = await screen.findByTestId('gate-tooltip-trigger-citation_pagerank');
    await user.hover(trigger);
    await waitFor(() => {
      expect(screen.getAllByText(/networkx/i).length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText(/paper_citations/i).length).toBeGreaterThan(0);
  });

  it('renders scoring weight sliders', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByLabelText(/embedding similarity weight/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/topic match weight/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/llm relevance weight/i)).toBeInTheDocument();
    });
  });

  it('renders Generate Pulse now button', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /generate pulse now/i })).toBeInTheDocument();
    });
  });
});
