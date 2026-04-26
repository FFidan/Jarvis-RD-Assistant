import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ContradictionsPanel } from '@/components/paper/ContradictionsPanel';
import { fetchContradictions, scanPaperContradictions } from '@/lib/api';

const mocks = vi.hoisted(() => ({
  trackExternalJob: vi.fn(),
  isRunning: vi.fn(() => false),
}));

vi.mock('@/lib/api', () => ({
  fetchContradictions: vi.fn(),
  scanPaperContradictions: vi.fn(),
}));

vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (state: unknown) => unknown) =>
    selector({
      trackExternalJob: mocks.trackExternalJob,
      isRunning: mocks.isRunning,
    }),
}));

const mockFetchContradictions = vi.mocked(fetchContradictions);
const mockScanPaperContradictions = vi.mocked(scanPaperContradictions);

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ContradictionsPanel paperId={42} />
    </QueryClientProvider>,
  );
}

describe('ContradictionsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.isRunning.mockReturnValue(false);
    mockFetchContradictions.mockResolvedValue({ contradictions: [], total: 0 });
  });

  it('shows a failed-request state instead of an empty state', async () => {
    mockFetchContradictions.mockRejectedValueOnce(new Error('backend down'));

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText('Failed to load contradictions.')).toBeInTheDocument();
    });
    expect(screen.queryByText('No verified contradictions found.')).not.toBeInTheDocument();
  });

  it('queues a paper-scoped scan and tracks the returned job', async () => {
    const user = userEvent.setup();
    mockScanPaperContradictions.mockResolvedValueOnce({ job_id: 'job-contradictions', status: 'queued' });

    renderPanel();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Scan contradictions' })).toBeEnabled();
    });
    await user.click(screen.getByRole('button', { name: 'Scan contradictions' }));

    expect(mockScanPaperContradictions).toHaveBeenCalledWith(42, { limit: 20 });
    await waitFor(() => {
      expect(mocks.trackExternalJob).toHaveBeenCalledWith({
        jobId: 'job-contradictions',
        kind: 'contradictions.scan',
        payload: { paper_id: 42, limit: 20 },
        status: 'queued',
      });
    });
  });
});
