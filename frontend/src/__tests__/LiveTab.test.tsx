import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

vi.mock('@/lib/logs', () => ({
  listEvents: vi.fn().mockResolvedValue({ events: [], next_cursor: null }),
  streamCorrelation: vi.fn().mockReturnValue({ close: vi.fn() }),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    listJobs: vi.fn().mockResolvedValue([]),
  };
});

import { LiveTab } from '@/components/logs/LiveTab';
import { listEvents, streamCorrelation } from '@/lib/logs';
import { listJobs } from '@/lib/api';

const mockListEvents = vi.mocked(listEvents);
const mockListJobs = vi.mocked(listJobs);
const mockStreamCorrelation = vi.mocked(streamCorrelation);

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <LiveTab />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('LiveTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders Running Jobs and Recent Events sections', () => {
    renderTab();
    expect(screen.getByText(/Running Jobs/i)).toBeInTheDocument();
    expect(screen.getByText(/Recent Events \(last 50\)/i)).toBeInTheDocument();
  });

  it('shows empty state when no running jobs', async () => {
    mockListJobs.mockResolvedValue([]);
    renderTab();
    await vi.waitFor(() => {
      expect(screen.getByText(/No jobs currently running/i)).toBeInTheDocument();
    });
  });

  it('shows empty state when no recent events', async () => {
    mockListEvents.mockResolvedValue({ events: [], next_cursor: null });
    renderTab();
    await vi.waitFor(() => {
      expect(screen.getByText(/No recent events/i)).toBeInTheDocument();
    });
  });

  it('renders recent events when data is available', async () => {
    mockListEvents.mockResolvedValue({
      events: [
        {
          id: 1,
          created_at: '2026-05-08T10:00:00Z',
          level: 'info',
          category: 'job',
          source: 'test-source',
          message: 'Test event message',
          context: {},
          correlation_id: null,
        },
      ],
      next_cursor: null,
    });
    renderTab();
    await vi.waitFor(() => {
      expect(screen.getByText('Test event message')).toBeInTheDocument();
    });
  });

  it('calls listJobs on mount', async () => {
    mockListJobs.mockResolvedValue([]);
    renderTab();
    await vi.waitFor(() => expect(mockListJobs).toHaveBeenCalledTimes(1));
  });

  it('calls listEvents on mount', async () => {
    mockListEvents.mockResolvedValue({ events: [], next_cursor: null });
    renderTab();
    await vi.waitFor(() => expect(mockListEvents).toHaveBeenCalledTimes(1));
  });

  it('closes SSE stream when component unmounts', async () => {
    const closeRef = vi.fn();
    mockStreamCorrelation.mockReturnValue({ close: closeRef });

    const job = {
      id: 'job-1',
      kind: 'test.job',
      status: 'running' as const,
      progress: 0,
      progress_message: null,
      result: null,
      error: null,
      created_at: '2026-05-08T10:00:00Z',
      started_at: '2026-05-08T10:00:01Z',
      finished_at: null,
      payload: { correlation_id: 'corr-abc' },
    };
    mockListJobs.mockResolvedValue([job]);

    const { unmount } = renderTab();

    // Wait for the job row to render
    await vi.waitFor(() => screen.getByText('test.job'));

    // Expand the job row to trigger SSE subscription
    const expandButton = screen.getByRole('button', { name: /test\.job/i });
    await act(async () => { expandButton.click(); });

    // SSE should be opened
    await vi.waitFor(() => expect(mockStreamCorrelation).toHaveBeenCalled());

    // Unmount → close should be called
    unmount();
    expect(closeRef).toHaveBeenCalled();
  });
});
