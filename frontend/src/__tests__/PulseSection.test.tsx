import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PulseSection } from '@/components/settings/PulseSection';

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
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.enabled', value: false },
      { key: 'pulse.cron', value: '0 4 * * *' },
      { key: 'pulse.deck_size', value: 10 },
      { key: 'pulse.stage2_top_k', value: 50 },
    ]);
    vi.mocked(fetchPulseStats).mockResolvedValue({
      window_days: 1,
      decks_generated: 3,
      avg_candidates: 42,
      avg_llm_calls: 15,
      avg_duration_s: 12.3,
      last_run_at: '2026-04-10T04:00:00Z',
      last_error: null,
      degraded_reason: null,
    });
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

  it('shows failed badge when pulse stats report an error', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      window_days: 1,
      decks_generated: 0,
      avg_candidates: null,
      avg_llm_calls: null,
      avg_duration_s: null,
      last_run_at: null,
      last_error: 'scoring pipeline exploded',
      degraded_reason: null,
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/scoring pipeline exploded/i)).toBeInTheDocument();
    });
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
