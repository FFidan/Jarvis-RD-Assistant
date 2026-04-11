import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AutomationSection } from '@/components/settings/AutomationSection';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchNudges: vi.fn(),
    updateNudge: vi.fn().mockResolvedValue({}),
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: '', value: null }),
    fetchPulseStats: vi.fn(),
  };
});

const { fetchNudges, fetchConfig, setConfig, fetchPulseStats } = await import('@/lib/api');

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AutomationSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AutomationSection — Pulse subsection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNudges).mockResolvedValue([]);
    vi.mocked(fetchConfig).mockResolvedValue([
      { key: 'pulse.enabled', value: false },
      { key: 'pulse.cron', value: '0 4 * * *' },
    ]);
    vi.mocked(fetchPulseStats).mockResolvedValue({
      window_days: 1,
      decks_generated: 3,
      avg_candidates: 42,
      avg_llm_calls: 15,
      avg_duration_s: 12.3,
      last_run_at: '2026-04-10T04:00:00Z',
      last_error: null,
    });
  });

  it('renders pulse enable toggle', async () => {
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

  it('renders last-run widget with stats data', async () => {
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/decks generated/i)).toBeInTheDocument();
    });
    // decks_generated=3 is displayed
    expect(screen.getByText(/3/)).toBeInTheDocument();
  });

  it('shows last error badge when pulse stats report an error', async () => {
    vi.mocked(fetchPulseStats).mockResolvedValue({
      window_days: 1,
      decks_generated: 0,
      avg_candidates: null,
      avg_llm_calls: null,
      avg_duration_s: null,
      last_run_at: null,
      last_error: 'scoring pipeline exploded',
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/scoring pipeline exploded/i)).toBeInTheDocument();
    });
  });
});
