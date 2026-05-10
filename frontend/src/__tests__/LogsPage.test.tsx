import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

vi.mock('@/lib/logs', () => ({
  listEvents: vi.fn().mockResolvedValue({ events: [], next_cursor: null }),
  getSummary: vi.fn().mockResolvedValue({ by_level: {}, by_category: {}, total: 0 }),
  getCorrelation: vi.fn().mockResolvedValue([]),
  getLogsSources: vi.fn().mockResolvedValue([]),
  streamCorrelation: vi.fn().mockReturnValue({ close: vi.fn() }),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    listJobs: vi.fn().mockResolvedValue([]),
    getPulseSourceHealth: vi.fn().mockResolvedValue([]),
    getPulseSourceHistory: vi.fn().mockResolvedValue({}),
  };
});

import { LogsPage } from '@/pages/LogsPage';

function renderPage(initialPath = '/logs') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <LogsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('LogsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders page heading', () => {
    renderPage();
    expect(screen.getByText('System Logs')).toBeInTheDocument();
  });

  it('renders all four tab triggers', () => {
    renderPage();
    expect(screen.getByRole('tab', { name: /live/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /jobs/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /sources/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /events/i })).toBeInTheDocument();
  });

  it('defaults to Live tab', () => {
    renderPage();
    const liveTab = screen.getByRole('tab', { name: /live/i });
    expect(liveTab).toHaveAttribute('data-state', 'active');
  });

  it('switches to Jobs tab when clicked', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('tab', { name: /jobs/i }));
    expect(screen.getByRole('tab', { name: /jobs/i })).toHaveAttribute('data-state', 'active');
  });

  it('switches to Sources tab when clicked', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('tab', { name: /sources/i }));
    expect(screen.getByRole('tab', { name: /sources/i })).toHaveAttribute('data-state', 'active');
  });

  it('switches to Events tab when clicked', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('tab', { name: /events/i }));
    expect(screen.getByRole('tab', { name: /events/i })).toHaveAttribute('data-state', 'active');
  });

  it('respects ?tab=jobs URL param', () => {
    renderPage('/logs?tab=jobs');
    const jobsTab = screen.getByRole('tab', { name: /jobs/i });
    expect(jobsTab).toHaveAttribute('data-state', 'active');
  });
});
